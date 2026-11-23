def convert_bbl_to_m3_per_hour(bbl_per_day):
    # Conversion factor from bbl/day to m^3/h
    conversion_factor = 0.00662447
    return bbl_per_day * conversion_factor

# Test the function
test_input = 1000
expected_output = 6.62447  # From the documents, 1000 bbl/day = 6.62447 m^3/h
tolerance = 1e-9

assert abs(convert_bbl_to_m3_per_hour(test_input) - expected_output) < tolerance
print(f"Test passed for input {test_input} bbl/day: {convert_bbl_to_m3_per_hour(test_input)} m^3/h")
