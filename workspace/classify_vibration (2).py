import math

def classify_vibration(vibration):
    if vibration <= 2.8:
        return "Zone A"
    elif vibration <= 4.5:
        return "Zone B"
    elif vibration <= 7.1:
        return "Zone C"
    else:
        return "Zone D"

# Test the function with the given examples
print(classify_vibration(2.1))  # Expected: Zone A
print(classify_vibration(4.5))  # Expected: Zone B
print(classify_vibration(6.4))  # Expected: Zone C
print(classify_vibration(7.5))  # Expected: Zone D
