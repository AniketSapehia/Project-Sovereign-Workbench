def psi_to_bar(psi):
    """
    Converts pressure from psi to bar.
    """
    return psi * 0.0689476

# Test the function
test_psi = 100
test_bar = psi_to_bar(test_psi)
print(f"{test_psi} psi is equal to {test_bar:.6f} bar")

# Check against the conversion factor
assert abs(test_bar - 6.89476) < 1e-9, "The conversion function is incorrect."
