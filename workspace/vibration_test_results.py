def classify_vibration(vibration_reading):
  if vibration_reading <= 2.8:
    return 'Zone A'
  elif vibration_reading <= 4.5:
    return 'Zone B'
  elif vibration_reading <= 7.1:
    return 'Zone C'
  else:
    return 'Zone D'

results = [classify_vibration(v) for v in [2.1, 4.5, 6.4, 7.5]]
results