from datetime import datetime

# Given values
original_thickness = 12.0
actual_thickness = 9.1
min_required_thickness = 8.0
years_in_service = datetime.now().year - 2016
previous_survey_years = 10

# Calculations
long_term_corrosion_rate = (original_thickness - actual_thickness) / years_in_service
short_term_corrosion_rate = (original_thickness - actual_thickness) / previous_survey_years if previous_survey_years > 0 else float('inf')
corrosion_rate = max(long_term_corrosion_rate, short_term_corrosion_rate)
remaining_life = (actual_thickness - min_required_thickness) / corrosion_rate
to_next_inspection = min(remaining_life, 0.5 * remaining_life)

# Results
corrosion_rate, remaining_life, to_next_inspection
