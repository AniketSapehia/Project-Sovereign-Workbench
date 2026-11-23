vibration_zone = 6.4
if vibration_zone > 2.8 and vibration_zone <= 4.5:
    zone = 'Zone B'
else:
    zone = 'Other'
print(f'Vibration Zone: {zone}')
print('Corrective Actions for Zone B:')
print('1. Increase monitoring to weekly')
print('2. Carry out spectrum analysis to find the cause')
print('3. Plan corrective maintenance within 30 days')