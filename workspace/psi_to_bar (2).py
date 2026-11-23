def psi_to_bar(psi):
    """Convert pressure from psi to bar."""
    return psi * 0.14503773800721

# Test the function with 100 psi
print(f"100 psi is approximately {psi_to_bar(100):.2f} bar.")
