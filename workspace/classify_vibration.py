import math

def classify_vibration(vibration):
    """
    Classifies a vibration reading into Zone A, B, C or D.
    """
    if vibration <= 2.8:
        return 'Zone A'
    elif 2.8 < vibration <= 4.5:
        return 'Zone B'
    elif 4.5 < vibration <= 7.1:
        return 'Zone C'
    else:
        return 'Zone D'

# Test cases
vibrations = [2.1, 4.5, 6.4, 7.5]
for vibration in vibrations:
    print(f'Vibration: {vibration} mm/s RMS, Zone: {classify_vibration(vibration)}')

# Example test cases against check values
# Zone A: 2.1 mm/s RMS
# Zone B: 4.5 mm/s RMS
# Zone C: 6.4 mm/s RMS
# Zone D: 7.5 mm/s RMS
