from datetime import datetime

# Given values
original_thickness = 12.0
actual_thickness = 9.1
min_required_thickness = 8.0
years_in_service = datetime.now().year - 2016

# Calculations
long_term_corrosion_rate = (original_thickness - actual_thickness) / years_in_service
remaining_life = (actual_thickness - min_required_thickness) / long_term_corrosion_rate
next_inspection_date = min(remaining_life / 2, 10)

corrosion_rate = max(long_term_corrosion_rate, (9.1 - 12.0) / (datetime.now().year - 2016))

print(f'Corrosion Rate: {corrosion_rate:.2f} mm/year')
print(f'Remaining Life: {remaining_life:.2f} years')
print(f'Next Inspection Date: {next_inspection_date:.0f} years')