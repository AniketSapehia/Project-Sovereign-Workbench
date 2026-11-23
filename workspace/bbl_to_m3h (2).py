def bbl_to_m3h(bbl_per_day):
    # Conversion factor from barrels per day to cubic metres per hour
    conversion_factor = 0.00662447
    return bbl_per_day * conversion_factor

# Test the function with 1000 bbl/day
test_input = 1000
test_output = bbl_to_m3h(test_input)
print(f"{test_input} bbl/day is approximately {test_output} m³/h")

# Verification with the provided check
assert abs(test_output - 6.62447) < 0.001, "The conversion result is incorrect."
