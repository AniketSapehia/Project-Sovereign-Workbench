t_actual = 10 # Example value for t_actual, actual thickness of the vessel
t_min = 5 # Example value for t_min, minimum thickness required
corrosion_rate = 0.1 # Example value for corrosion rate, rate of thickness decrease

# Calculate the remaining life
remaining_life = (t_actual - t_min) / corrosion_rate

# Determine if an approval note is needed
if remaining_life < 4:
    print("An approval note for repair or replacement planning must be raised in the next turnaround.")
else:
    print("No approval note is needed as the remaining life is more than 4 years.")