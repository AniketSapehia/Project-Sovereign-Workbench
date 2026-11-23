def bbl_to_m3h(bbl_day):
    # Conversion factor from barrels per day to cubic metres per hour
    conversion_factor = 0.00662447
    # Perform the conversion
    m3h = bbl_day * conversion_factor
    return m3h

# Test the function with 1000 bbl/day
test_bbl_day = 1000
expected_m3h = 6.62447  # According to REF-ENG-001
result_m3h = bbl_to_m3h(test_bbl_day)

# Check the result
assert abs(result_m3h - expected_m3h) < 1e-9, f"Expected {expected_m3h}, but got {result_m3h}"

print(f"1000 bbl/day converts to {result_m3h} m³/h.")
