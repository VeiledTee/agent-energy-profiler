### idle  (NVIDIA GeForce RTX 4090, driver 610.88, 4 blocks x 8.0 s, temperature [38, 39] C)

| condition | reads/s | counter W (95% CI) | retained vs endpoints, mean (95% CI) | median | power W | counter / integrated power | counter updates | P-state changed |
|---|---|---|---|---|---|---|---|---|
| endpoints | 0.0 | 27.50 ± 0.61 | 1.000 ± 0.000 | 1.000 |  |  |  |  |
| energy_1hz | 0.9 | 27.35 ± 0.17 | 0.995 ± 0.025 | 0.999 |  |  | every 999.4 ms, 27950 mJ |  |
| power_instant_10hz | 10.0 | 24.41 ± 0.39 | 0.888 ± 0.016 | 0.886 | 28.48 | 0.857 ± 0.010 |  |  |
| energy_10hz | 10.0 | 24.57 ± 0.24 | 0.894 ± 0.013 | 0.897 |  |  | every 100.2 ms, 2568 mJ |  |
| temperature_10hz | 10.0 | 27.57 ± 0.19 | 1.003 ± 0.026 | 1.008 |  |  |  |  |
| power_avg_10hz | 10.0 | 30.16 ± 8.45 | 1.095 ± 0.284 | 1.011 | 30.80 | 0.979 ± 0.022 |  | 1 |
| energy_field_10hz | 10.0 | 24.41 ± 0.46 | 0.888 ± 0.021 | 0.884 |  |  | every 100.5 ms, 2556 mJ |  |
| combined_10hz | 20.0 | 24.36 ± 0.41 | 0.886 ± 0.022 | 0.883 | 28.38 | 0.858 ± 0.010 | every 100.4 ms, 2556 mJ |  |
| energy_50hz | 49.9 | 12.18 ± 0.27 | 0.443 ± 0.016 | 0.442 |  |  | every 100.8 ms, 1348 mJ |  |
| power_avg_50hz | 49.9 | 27.60 ± 0.23 | 1.004 ± 0.026 | 1.002 | 28.49 | 0.969 ± 0.010 |  |  |
| energy_200hz | 193.2 | 3.78 ± 0.25 | 0.138 ± 0.012 | 0.137 |  |  | every 103.1 ms, 392 mJ |  |
| energy_1000hz | 665.6 | 0.93 ± 0.19 | 0.034 ± 0.007 | 0.034 |  |  | every 103.1 ms, 76 mJ |  |

Endpoints-only counter W per block: [27.602, 27.983, 27.113, 27.295]. Carry-over check, endpoints trials right after >= 50 Hz energy polling: [27.295]; after anything else: [27.602, 27.983, 27.113].

### steady  (NVIDIA GeForce RTX 4090, driver 610.88, 4 blocks x 8.0 s, temperature [68, 79] C)

| condition | reads/s | counter W (95% CI) | retained vs endpoints, mean (95% CI) | median | power W | counter / integrated power | counter updates | P-state changed |
|---|---|---|---|---|---|---|---|---|
| endpoints | 0.0 | 430.99 ± 5.25 | 1.000 ± 0.000 | 1.000 |  |  |  |  |
| energy_1hz | 0.9 | 427.14 ± 7.43 | 0.991 ± 0.019 | 0.992 |  |  | every 998.8 ms, 439238 mJ |  |
| energy_field_10hz | 10.0 | 386.12 ± 4.58 | 0.896 ± 0.021 | 0.898 |  |  | every 100.1 ms, 40036 mJ |  |
| energy_10hz | 10.0 | 385.48 ± 3.55 | 0.894 ± 0.003 | 0.894 |  |  | every 100.1 ms, 40070 mJ |  |
| power_instant_10hz | 10.0 | 386.65 ± 6.10 | 0.897 ± 0.018 | 0.899 | 445.83 | 0.867 ± 0.008 |  |  |
| power_avg_10hz | 10.0 | 430.71 ± 5.66 | 0.999 ± 0.002 | 0.999 | 445.76 | 0.966 ± 0.012 |  |  |
| temperature_10hz | 10.0 | 432.15 ± 4.36 | 1.003 ± 0.012 | 1.000 |  |  |  |  |
| combined_10hz | 20.0 | 385.68 ± 3.43 | 0.895 ± 0.012 | 0.898 | 446.49 | 0.864 ± 0.009 | every 100.1 ms, 40431 mJ |  |
| power_avg_50hz | 49.9 | 432.38 ± 3.63 | 1.003 ± 0.009 | 1.003 | 445.87 | 0.970 ± 0.011 |  |  |
| energy_50hz | 49.9 | 205.55 ± 4.29 | 0.477 ± 0.010 | 0.477 |  |  | every 100.6 ms, 21999 mJ |  |
| energy_200hz | 195.4 | 81.28 ± 3.05 | 0.189 ± 0.005 | 0.188 |  |  | every 103.0 ms, 8753 mJ |  |
| energy_1000hz | 726.3 | 16.26 ± 2.67 | 0.038 ± 0.007 | 0.039 |  |  | every 103.2 ms, 1426 mJ |  |

Endpoints-only counter W per block: [432.002, 427.538, 429.333, 435.105]. Carry-over check, endpoints trials right after >= 50 Hz energy polling: [435.105]; after anything else: [432.002, 427.538, 429.333].

### pulsed  (NVIDIA GeForce RTX 4090, driver 610.88, 4 blocks x 8.0 s, temperature [49, 69] C)

| condition | reads/s | counter W (95% CI) | retained vs endpoints, mean (95% CI) | median | power W | counter / integrated power | counter updates | P-state changed |
|---|---|---|---|---|---|---|---|---|
| endpoints | 0.0 | 253.90 ± 2.24 | 1.000 ± 0.000 | 1.000 |  |  |  |  |
| energy_1hz | 0.9 | 253.02 ± 6.38 | 0.997 ± 0.030 | 0.991 |  |  | every 999.3 ms, 255969 mJ |  |
| energy_10hz | 10.0 | 229.38 ± 5.82 | 0.903 ± 0.027 | 0.906 |  |  | every 100.5 ms, 25078 mJ |  |
| temperature_10hz | 10.0 | 259.07 ± 4.27 | 1.020 ± 0.020 | 1.021 |  |  |  |  |
| power_instant_10hz | 10.0 | 229.47 ± 7.39 | 0.904 ± 0.030 | 0.902 | 266.35 | 0.863 ± 0.027 |  |  |
| energy_field_10hz | 10.0 | 229.46 ± 4.58 | 0.904 ± 0.012 | 0.903 |  |  | every 100.4 ms, 22722 mJ |  |
| power_avg_10hz | 10.0 | 260.80 ± 5.89 | 1.027 ± 0.023 | 1.023 | 266.94 | 0.977 ± 0.023 |  |  |
| combined_10hz | 20.0 | 227.28 ± 3.40 | 0.895 ± 0.020 | 0.893 | 267.22 | 0.851 ± 0.016 | every 100.2 ms, 26535 mJ |  |
| energy_50hz | 49.1 | 143.21 ± 9.53 | 0.564 ± 0.040 | 0.567 |  |  | every 100.7 ms, 10882 mJ |  |
| power_avg_50hz | 49.8 | 255.38 ± 5.97 | 1.006 ± 0.030 | 1.005 | 265.92 | 0.960 ± 0.019 |  |  |
| energy_200hz | 174.3 | 63.02 ± 2.38 | 0.248 ± 0.011 | 0.251 |  |  | every 103.8 ms, 5032 mJ |  |
| energy_1000hz | 548.5 | 37.02 ± 2.56 | 0.146 ± 0.011 | 0.145 |  |  | every 103.3 ms, 1770 mJ |  |

Endpoints-only counter W per block: [252.907, 254.114, 252.775, 255.812]. Carry-over check, endpoints trials right after >= 50 Hz energy polling: [255.812]; after anything else: [252.907, 254.114, 252.775].

