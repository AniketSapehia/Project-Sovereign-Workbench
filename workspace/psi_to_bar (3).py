def psi_to_bar(psi):
    # Conversion factor from psi to bar: 1 psi = 0.0689476 bar
    return psi * 0.0689476

# Test the function on 100 psi
print(psi_to_bar(100))

# Test cases to verify the function
assert abs(psi_to_bar(100) - 6.89476) < 0.001
assert abs(psi_to_bar(14.5038) - 1) < 0.001
