# Define a function to convert miles to kilometers
def miles_to_kilometers(miles):
    # Conversion factor from miles to kilometers
    conversion_factor = 1.60934
    # Convert the given miles to kilometers
    kilometers = miles * conversion_factor
    return kilometers

# Test the function with a known value
miles = 10
expected_kilometers = 16.0934  # This is the expected value based on the conversion factor
assert abs(miles_to_kilometers(miles) - expected_kilometers) < 0.0001, "The conversion is incorrect."

# Print the result of the test
print(f"Test Result: {miles_to_kilometers(miles)} kilometers")
