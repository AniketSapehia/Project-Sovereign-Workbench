def psi_to_bar(psi):
    return psi * 0.0689476

# Test the function with the provided check
check_psi = 100
check_bar = 6.89476
TOLERANCE = 1e-6

assert abs(psi_to_bar(check_psi) - check_bar) < TOLERANCE, "Test failed for psi_to_bar"

# Test the function with 100 psi
result = psi_to_bar(100)
print(f"100 psi is {result} bar")
