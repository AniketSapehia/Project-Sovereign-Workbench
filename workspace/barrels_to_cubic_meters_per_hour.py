def barrels_to_cubic_meters_per_hour(barrels_per_day):
    # 1 barrel (oil, bbl) = 158.987 litres
    # 1 m³/h = 4.40287 US gpm
    # 1 barrel/day = 0.00662447 m³/h
    return barrels_per_day * 0.00662447

# Test the function with 1000 bbl/day
check_input = 1000
check_output = 6.62447
tolerance = 1e-9

assert abs(barrels_to_cubic_meters_per_hour(check_input) - check_output) < tolerance

print(f"{check_input} bbl/day is equal to {barrels_to_cubic_meters_per_hour(check_input):.4f} m³/h")
