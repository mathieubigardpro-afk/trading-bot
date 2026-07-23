"""RiskManager — pipeline de risque du bot (calibrage AGRESSIF, profil du projet).

Interface contractuelle (voir `docs/ARCHITECTURE.md`, section "bot/risk/") :

    apply(cibles_brutes: dict[str, float], state: dict, prices: dict[str, Quote|None],
          history: dict[str, pd.DataFrame]) -> tuple[dict[str, float], dict[str, str]]

Pipeline appliqué à chaque cycle, dans cet ordre :

  1. Estimation de l'équity courante (cash + positions valorisées au mid courant, ou au coût
     moyen si le prix est indisponible ce cycle) et mise à jour du pic historique
     `equity_peak_usd` (jamais redescendu, seulement remonté).
  2. Circuit breakers (`circuit_breakers.py`) : perte 24h glissante, cooldown pertes
     consécutives, drawdown depuis le pic -> flags `freeze_entries` / `half_size` / `flatten` /
     `manual_review`, qui contraignent tout le reste du pipeline. État persisté dans
     `state["circuit_breakers"]`.
  3. Vol targeting PORTEFEUILLE (`vol_targeting.py`) : un scalaire unique, appliqué à TOUTES les
     cibles brutes simultanément (pas de vol targeting actif par actif — voir justification
     dans `vol_targeting.py`).
  4. Par actif : garde-fou prix indisponible (poids figé, jamais de trade) ; gel des nouvelles
     entrées si breaker actif (bloque seulement le RENFORCEMENT, jamais une réduction/sortie) ;
     demi-taille si breaker actif ; cap individuel par classe d'actif (25% crypto / 15% action) ;
     flatten total si breaker de drawdown sévère actif (prioritaire sur tout le reste).
  5. Cap d'exposition brute totale (80% par défaut), réparti au prorata entre tous les actifs
     positifs en cas de dépassement après les étapes précédentes.
  6. No-trade band (±5 points de %, en fraction d'équity) : un écart entre la cible finale et
     le poids actuellement détenu inférieur à la bande n'est jamais exécuté — la cible RENVOYÉE
     est alors le poids ACTUEL inchangé (pas la valeur "cible" calculée), sauf en `flatten_mode`
     où le flatten est toujours exécuté indépendamment de la bande.

     Bande PAR SYMBOLE (correctif audit — cf. `apply(..., no_trade_band_by_symbol=...)`) :
     `no_trade_band` (ex. 5%) est un réglage de profil PENSÉ EN FRACTION DE LA POCHE qui porte
     chaque actif, jamais de l'équity TOTALE du wallet. Un book équipondéré `top_k=10` mis à
     l'échelle par `capital_alloc_pct=35%` donne un poids INTRINSÈQUE par titre de 3.5% de
     l'équity du wallet — TOUJOURS sous une bande de 5% appliquée telle quelle au niveau
     portefeuille, ce qui gelait silencieusement la poche actions à 0% en permanence (bug
     constaté et corrigé). L'appelant (`bot/runner.py`) peut donc fournir
     `no_trade_band_by_symbol: dict[symbole, alloc_de_la_poche_0..1]` : la bande RÉELLEMENT
     appliquée à ce symbole devient alors `no_trade_band * no_trade_band_by_symbol[symbole]`
     (bande exprimée en fraction de la poche, puis re-projetée en fraction d'équity totale via
     le même facteur d'échelle que les cibles elles-mêmes). Un symbole absent du mapping garde
     le comportement historique (`no_trade_band` brut, en fraction d'équity totale) — compatible
     avec tous les appels existants qui n'ont jamais fourni ce paramètre.

Notes de conception :
  - Le filtre de régime SMA200/ATR14 percentile (§3D du rapport de recherche, cité comme étape
    4 du pipeline générique dans `docs/ARCHITECTURE.md`) N'EST PAS implémenté dans ce module :
    la spécification de CE deliverable ("TON MODULE" dans les instructions de la tâche) ne le
    liste pas parmi les responsabilités attendues de `bot/risk`, et le rapport de recherche
    (§2C) le décrit comme une condition intégrée au signal de la stratégie mean-reversion
    elle-même (`RSI(2) < 10 ET prix > SMA200` fait partie de la définition du signal, en amont
    de `cibles_brutes` reçu par `apply()`), pas comme une étape générique de sizing post-signal.
    Documenté explicitement ici pour qu'aucun intégrateur ne suppose silencieusement une gate
    de régime supplémentaire côté risque.
  - "Poids" = fraction de l'équity courante (toujours >= 0, univers long-only strict).
  - `apply()` MODIFIE `state` en place (met à jour `equity_peak_usd`/`equity_peak_ts` et
    `circuit_breakers`) en plus de retourner les cibles finales — c'est volontaire et conforme
    à "l'état des breakers est lu/écrit dans state.json" : c'est à l'appelant (`runner.py`,
    hors périmètre de ce module) de persister `state` via `bot.persist.save_state()` après un
    cycle complet et réussi.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from . import circuit_breakers as cb_mod
from . import vol_targeting as vol_mod
from .config_fallback import cfg as _DEFAULT_CFG


class RiskManager:
    def __init__(
        self,
        vol_target_annualized: float = _DEFAULT_CFG.VOL_TARGET_ANNUALIZED,
        vol_ewma_halflife_hours: float = _DEFAULT_CFG.VOL_EWMA_HALFLIFE_HOURS,
        vol_coldstart_min_points: int = _DEFAULT_CFG.VOL_COLDSTART_MIN_POINTS,
        vol_coldstart_scalar: float = _DEFAULT_CFG.VOL_COLDSTART_SCALAR,
        cap_per_asset_crypto: float = _DEFAULT_CFG.CAP_PER_ASSET_CRYPTO,
        cap_per_asset_equity: float = _DEFAULT_CFG.CAP_PER_ASSET_EQUITY,
        gross_exposure_max: float = _DEFAULT_CFG.GROSS_EXPOSURE_MAX,
        no_trade_band: float = _DEFAULT_CFG.NO_TRADE_BAND,
        cb_daily_loss_freeze_pct: float = _DEFAULT_CFG.CB_DAILY_LOSS_FREEZE_PCT,
        cb_daily_loss_freeze_hours: float = _DEFAULT_CFG.CB_DAILY_LOSS_FREEZE_HOURS,
        cb_consecutive_losses_trigger: int = _DEFAULT_CFG.CB_CONSECUTIVE_LOSSES_TRIGGER,
        cb_cooldown_hours: float = _DEFAULT_CFG.CB_COOLDOWN_HOURS,
        cb_dd_half_size_pct: float = _DEFAULT_CFG.CB_DD_HALF_SIZE_PCT,
        cb_dd_flatten_pct: float = _DEFAULT_CFG.CB_DD_FLATTEN_PCT,
        symbols_crypto: Optional[list] = None,
        symbols_equity: Optional[list] = None,
    ):
        if not (0 < vol_target_annualized <= 50):
            # Borne large (pas seulement 0.25-0.30) : les tests ont légitimement besoin de
            # neutraliser le vol targeting (scalar ~1.0) en fixant une cible très supérieure à
            # toute vol réaliste, pour isoler d'autres étapes du pipeline (caps, no-trade band).
            raise ValueError(f"vol_target_annualized hors plage plausible: {vol_target_annualized}")
        if cb_dd_half_size_pct >= cb_dd_flatten_pct:
            raise ValueError(
                "cb_dd_half_size_pct doit être strictement inférieur à cb_dd_flatten_pct "
                f"(reçu {cb_dd_half_size_pct} >= {cb_dd_flatten_pct})"
            )

        self.vol_target_annualized = float(vol_target_annualized)
        self.vol_ewma_halflife_hours = float(vol_ewma_halflife_hours)
        self.vol_coldstart_min_points = int(vol_coldstart_min_points)
        self.vol_coldstart_scalar = float(vol_coldstart_scalar)
        self.cap_per_asset_crypto = float(cap_per_asset_crypto)
        self.cap_per_asset_equity = float(cap_per_asset_equity)
        self.gross_exposure_max = float(gross_exposure_max)
        self.no_trade_band = float(no_trade_band)
        self.cb_daily_loss_freeze_pct = float(cb_daily_loss_freeze_pct)
        self.cb_daily_loss_freeze_hours = float(cb_daily_loss_freeze_hours)
        self.cb_consecutive_losses_trigger = int(cb_consecutive_losses_trigger)
        self.cb_cooldown_hours = float(cb_cooldown_hours)
        self.cb_dd_half_size_pct = float(cb_dd_half_size_pct)
        self.cb_dd_flatten_pct = float(cb_dd_flatten_pct)
        self.symbols_crypto = list(symbols_crypto or _DEFAULT_CFG.SYMBOLS_CRYPTO)
        self.symbols_equity = list(symbols_equity or _DEFAULT_CFG.SYMBOLS_EQUITY)

    # ------------------------------------------------------------------ helpers

    def _asset_cap(self, symbol: str) -> float:
        if symbol in self.symbols_crypto:
            return self.cap_per_asset_crypto
        if symbol in self.symbols_equity:
            return self.cap_per_asset_equity
        # Actif inconnu de l'univers déclaré : cap le plus conservateur des deux (pessimisme).
        return min(self.cap_per_asset_crypto, self.cap_per_asset_equity)

    @staticmethod
    def _mark_price(symbol: str, prices: Optional[dict], positions: dict) -> Optional[float]:
        quote = prices.get(symbol) if prices else None
        if quote is not None:
            mid = getattr(quote, "mid", None)
            if mid is not None:
                try:
                    mid_f = float(mid)
                    if mid_f > 0:
                        return mid_f
                except (TypeError, ValueError):
                    pass
        # Prix frais indisponible ce cycle : repli sur le coût moyen d'achat UNIQUEMENT pour
        # estimer l'équity servant aux circuit breakers (jamais pour un fill réel, qui reste
        # de la stricte responsabilité de bot.sim.ExchangeSim avec ses propres règles).
        pos = positions.get(symbol) if positions else None
        if pos is not None:
            prix_moyen = pos.get("prix_moyen")
            if prix_moyen:
                try:
                    return float(prix_moyen)
                except (TypeError, ValueError):
                    return None
        return None

    def _estimate_equity_and_weights(
        self, state: dict, prices: Optional[dict]
    ) -> Tuple[float, Dict[str, float]]:
        cash = float(state.get("cash_usd", 0.0) or 0.0)
        positions = state.get("positions", {}) or {}
        marks: Dict[str, float] = {}
        total = cash
        for symbol, pos in positions.items():
            qty = float(pos.get("qty", 0.0) or 0.0)
            if qty <= 0:
                continue
            mark = self._mark_price(symbol, prices, positions)
            if mark is None:
                continue  # ni prix frais ni coût moyen exploitable : exclu de l'estimation
            marks[symbol] = mark
            total += qty * mark
        equity = max(total, 0.0)
        weights: Dict[str, float] = {}
        if equity > 0:
            for symbol, pos in positions.items():
                qty = float(pos.get("qty", 0.0) or 0.0)
                if qty <= 0 or symbol not in marks:
                    continue
                weights[symbol] = (qty * marks[symbol]) / equity
        return equity, weights

    # ------------------------------------------------------------------ pipeline

    def apply(
        self,
        cibles_brutes: Dict[str, float],
        state: dict,
        prices: Optional[dict],
        history: Optional[dict],
        now: Optional[datetime] = None,
        no_trade_band_by_symbol: Optional[Dict[str, float]] = None,
    ) -> Tuple[Dict[str, float], Dict[str, str]]:
        """`no_trade_band_by_symbol` (optionnel) : voir docstring de tête de ce module, étape 6
        ("Bande PAR SYMBOLE"). `{symbole: alloc}` avec `alloc` dans `[0, 1]` = part du capital
        du wallet allouée à la POCHE qui porte ce symbole (`capital_alloc_pct`) ; la bande
        effectivement appliquée à ce symbole est alors `self.no_trade_band * alloc` au lieu de
        `self.no_trade_band` brut. Symbole absent -> comportement historique inchangé."""
        now = now or datetime.now(timezone.utc)
        prices = prices or {}
        history = history or {}
        cibles_brutes = dict(cibles_brutes or {})
        no_trade_band_by_symbol = no_trade_band_by_symbol or {}

        # --- 1) équity courante + pic ---
        equity_now, poids_actuel = self._estimate_equity_and_weights(state, prices)

        equity_peak = float(state.get("equity_peak_usd") or 0.0)
        if equity_now > equity_peak:
            equity_peak = equity_now
            state["equity_peak_usd"] = equity_peak
            state["equity_peak_ts"] = now.isoformat()
        elif "equity_peak_usd" not in state:
            state["equity_peak_usd"] = equity_peak
            state["equity_peak_ts"] = now.isoformat()

        # --- 2) circuit breakers ---
        cb_state_in = state.get("circuit_breakers") or {}
        cb_state_out, flags = cb_mod.evaluate_breakers(
            cb_state_in,
            now,
            equity_now,
            equity_peak,
            self.cb_daily_loss_freeze_pct,
            self.cb_daily_loss_freeze_hours,
            self.cb_consecutive_losses_trigger,
            self.cb_cooldown_hours,
            self.cb_dd_half_size_pct,
            self.cb_dd_flatten_pct,
        )
        state["circuit_breakers"] = cb_state_out

        # --- 3) vol targeting portefeuille ---
        vol_annual, coldstart = vol_mod.portfolio_vol_annualized(
            history, cibles_brutes, self.vol_ewma_halflife_hours, self.vol_coldstart_min_points
        )
        vol_scalar = vol_mod.compute_vol_scalar(
            vol_annual, self.vol_target_annualized, coldstart, self.vol_coldstart_scalar
        )

        universe = set(cibles_brutes.keys()) | set(poids_actuel.keys())
        interim: Dict[str, float] = {}
        reasons: Dict[str, str] = {}

        for symbol in universe:
            current_w = poids_actuel.get(symbol, 0.0)

            # --- flatten prioritaire sur tout (breaker DD > seuil flatten) ---
            if flags["flatten"]:
                interim[symbol] = 0.0
                reasons[symbol] = (
                    "flatten_mode actif (drawdown > seuil flatten) — position mise à plat, "
                    "revue manuelle requise"
                )
                continue

            raw = cibles_brutes.get(symbol)
            quote = prices.get(symbol)
            price_available = quote is not None and getattr(quote, "mid", None) not in (None, 0)

            # --- garde-fou prix indisponible : poids figé, quelle que soit la cible brute ---
            if not price_available:
                interim[symbol] = current_w
                reasons[symbol] = "prix indisponible ce cycle — poids figé (garde-fou pessimiste)"
                continue

            if raw is None:
                interim[symbol] = current_w
                reasons[symbol] = "aucune cible brute fournie pour cet actif — poids conservé"
                continue

            target = float(raw) * vol_scalar
            reason_parts = ["vol targeting appliqué"]

            # --- gel des nouvelles entrées : bloque seulement le renforcement ---
            if flags["freeze_entries"] and target > current_w:
                target = current_w
                reason_parts = ["gel des nouvelles entrées actif — renforcement bloqué"]

            # --- demi-taille (drawdown > seuil half-size) ---
            if flags["half_size"]:
                target *= 0.5
                reason_parts.append("demi-taille (drawdown > seuil half-size)")

            # --- cap individuel par classe d'actif ---
            cap = self._asset_cap(symbol)
            if target > cap:
                target = cap
                reason_parts.append(f"plafonné au cap par actif ({cap:.0%})")

            if target < 0:
                target = 0.0

            interim[symbol] = target
            reasons[symbol] = " ; ".join(reason_parts)

        # --- 5) cap d'exposition brute totale, prorata si dépassement ---
        gross = sum(abs(w) for w in interim.values())
        if gross > self.gross_exposure_max and gross > 0:
            scale = self.gross_exposure_max / gross
            for symbol in list(interim.keys()):
                if interim[symbol] > 0:
                    interim[symbol] *= scale
                    reasons[symbol] += (
                        f" ; réduit au prorata pour respecter le cap d'exposition brute "
                        f"({self.gross_exposure_max:.0%})"
                    )

        # --- 6) no-trade band ---
        cibles_finales: Dict[str, float] = {}
        for symbol in universe:
            current_w = poids_actuel.get(symbol, 0.0)
            target = interim.get(symbol, current_w)
            if flags["flatten"]:
                # Le flatten s'exécute toujours, même pour un écart < bande.
                cibles_finales[symbol] = target
                continue
            band_scale = no_trade_band_by_symbol.get(symbol)
            band = self.no_trade_band if band_scale is None else self.no_trade_band * float(band_scale)
            if abs(target - current_w) < band:
                cibles_finales[symbol] = current_w
                reasons[symbol] = (
                    reasons.get(symbol, "")
                    + f" ; no-trade band : écart < {band:.2%} (poche), poids actuel conservé"
                )
            else:
                cibles_finales[symbol] = target

        return cibles_finales, reasons


def apply(
    cibles_brutes: Dict[str, float],
    state: dict,
    prices: Optional[dict],
    history: Optional[dict],
    now: Optional[datetime] = None,
) -> Tuple[Dict[str, float], Dict[str, str]]:
    """Fonction module-level de commodité (voir `bot/risk/__init__.py`) — instancie un
    `RiskManager` avec les défauts AGRESSIFS du projet (`config_fallback.py`) et délègue.
    Compatible avec l'appel décrit dans `docs/ARCHITECTURE.md` (`bot.risk.apply(...)`)."""
    return RiskManager().apply(cibles_brutes, state, prices, history, now=now)
