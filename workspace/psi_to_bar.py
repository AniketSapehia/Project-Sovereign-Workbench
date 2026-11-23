def psi_to_bar(psi):
    """
    Convert pressure from pounds per square inch (psi) to bars.
    
    Parameters:
    psi (float): The pressure in pounds per square inch.
    
    Returns:
    float: The pressure in bars.
    """
    bar = psi / 68.1
    return bar

# Test the function with 100 psi
psi = 100
bar = psi_to_bar(psi)
print(f"{psi} psi is equal to {bar} bars.")
