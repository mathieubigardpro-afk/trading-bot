"""Régression : `bot.config.HISTORY_N_HOURS` doit toujours suffire à obtenir
`bot.strategies.quasi_passif_crypto.REGIME_SMA_DAYS` (200) jours calendaires COMPLETS via
`_daily_closes()`, quelle que soit l'heure UTC à laquelle le cycle horaire s'exécute.

Trouvé lors de l'intégration des stratégies (docs/ARCHITECTURE.md §11) : une fenêtre de
EXACTEMENT `REGIME_SMA_DAYS*24` heures peut perdre jusqu'à 46h aux deux bords (jour courant
toujours partiel + jour le plus ancien de la fenêtre rarement aligné sur minuit UTC), ramenant
le nombre de jours complets disponibles à 199 au lieu de 200 selon l'heure du cycle — la SMA200
de `quasi_passif_crypto` deviendrait alors structurellement incalculable à certaines heures,
malgré un historique par ailleurs suffisant en apparence (4800 bougies horaires)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from bot import config
from bot.strategies.quasi_passif_crypto import REGIME_SMA_DAYS, _daily_closes, _is_trend_on


def _hourly_closes_ending_at(end_exclusive_hour: datetime, n_hours: int) -> pd.DataFrame:
    idx = pd.date_range(end=end_exclusive_hour - timedelta(hours=1), periods=n_hours, freq="h", tz="UTC")
    closes = [100.0 + i * 0.01 for i in range(n_hours)]  # tendance haussière triviale
    return pd.DataFrame({"close": closes}, index=idx)


@pytest.mark.parametrize("hour_of_day", list(range(24)))
def test_history_n_hours_always_yields_sma200_regardless_of_cycle_hour(hour_of_day):
    now = datetime(2026, 7, 22, hour_of_day, 0, tzinfo=timezone.utc)
    df = _hourly_closes_ending_at(now, config.HISTORY_N_HOURS)

    daily = _daily_closes(df)
    assert len(daily) >= REGIME_SMA_DAYS, (
        f"heure de cycle {hour_of_day:02d}h UTC : seulement {len(daily)} jours complets "
        f"disponibles avec HISTORY_N_HOURS={config.HISTORY_N_HOURS} (besoin >= {REGIME_SMA_DAYS})"
    )
    assert _is_trend_on(daily) is not None
