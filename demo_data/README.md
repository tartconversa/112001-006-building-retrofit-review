# Demo inputs for the retrofit review app.
# Every record is fictional. Load with:
#   python -m retrofit.server --seed demo_data
#
# Scenarios covered:
#   BL01 / PR01  completed, verified saving, comfort passes  -> priority
#   BL02 / PR02  completed, energy fall but comfort fails    -> service_fail
#   BL03 / PR03  use type changed mid-comparison             -> incomparable
#   BL04 / PR04  completed, survey interval crosses target   -> needs_measurement
#   BL02 / PR05  candidate, service baseline unproven        -> needs_measurement
#   BL01 / PR06  candidate, closure > 25% of works           -> defer
#   BL04 / PR07  candidate with strong evidence              -> conditional
#
# See buildings.csv, energy_weather.csv, comfort_projects.csv,
# service_targets.csv, retrofit_projects.csv, site_events.csv and
# commissioning_measurements.csv for the fields.
