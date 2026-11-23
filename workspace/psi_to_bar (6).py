def psi_to_bar(psi):
    # Conversion factor from psi to bar
    conversion_factor = 0.0689476
    # Convert psi to bar
    bar = psi * conversion_factor
    return bar

# Test the function
test_psi = 100
expected_bar = 6.89476  # According to the documents
result_bar = psi_to_bar(test_psi)
print(f"{test_psi} psi is equal to {result_bar} bar")

# Check if the result is within the tolerance
tolerance = 0.0001
assert abs(result_bar - expected_bar) < tolerance, "The conversion is incorrect"
