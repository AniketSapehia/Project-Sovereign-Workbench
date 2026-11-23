def bbl_to_m3_per_h(bbl_per_day):
    return bbl_per_day * 0.00662447

# Test the function with the provided check
check_bbl_per_day = 1000
check_m3_per_h = 6.62447
TOLERANCE = 1e-6

assert abs(bbl_to_m3_per_h(check_bbl_per_day) - check_m3_per_h) < TOLERANCE, "Test failed for bbl_to_m3_per_h"

# Test the function with 1000 bbl/day
result = bbl_to_m3_per_h(1000)
print(f"1000 bbl/day is {result} m³/h")
