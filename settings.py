"""App-wide defaults."""

DEFAULT_BATTERY_KWH = 75.0
BATTERY_KWH_MIN = 30.0
BATTERY_KWH_MAX = 120.0
BATTERY_KWH_STEP = 0.5

# Flat $/kWh rate applied to every charging stop for the cost estimate.
# 0.45 is roughly the US Tesla Supercharger average (state averages sit
# in the ~$0.36-0.47 range). No authoritative per-station source exists
# on a free tier — user can override via the sidebar.
DEFAULT_CHARGE_RATE_USD_PER_KWH = 0.45
CHARGE_RATE_MIN = 0.10
CHARGE_RATE_MAX = 1.50
CHARGE_RATE_STEP = 0.01

ABRP_SHEET_NAME = "ABRP Plan"
