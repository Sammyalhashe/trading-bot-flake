# Moving Average Ratio Benchmark Results

Generated on: 2026-04-02

## Overview
This benchmark evaluated different Moving Average (MA) windows for the `trend_following` and `auto` strategies across multiple timeframes (1h, 15m) and recent market conditions (H2 2024, YTD 2025).

## Top 10 Configurations (by Return)

| strategy                 | timeframe   | period   |   total_return_pct |   sharpe_ratio |   max_drawdown_pct |
|:-------------------------|:------------|:---------|-------------------:|---------------:|-------------------:|
| trend_following_MA21_55  | 1h          | H2_2024  |           14.7237  |       2.62152  |           -4.2763  |
| trend_following_MA20_100 | 1h          | H2_2024  |           14.249   |       2.53418  |           -5.57991 |
| trend_following_MA20_50  | 1h          | H2_2024  |           13.2991  |       2.39593  |           -5.10947 |
| trend_following_MA9_21   | 1h          | H2_2024  |           12.1912  |       2.22942  |           -5.73537 |
| trend_following_MA10_30  | 1h          | H2_2024  |           12.0762  |       2.21824  |           -5.83287 |
| trend_following_MA20_50  | 15m         | H2_2024  |            9.23801 |       0.929947 |           -5.18502 |
| auto_MA20_100            | 1h          | H2_2024  |            7.83307 |       1.38839  |           -7.67334 |
| trend_following_MA21_55  | 15m         | H2_2024  |            7.82751 |       0.821162 |           -4.78528 |
| auto_MA10_30             | 1h          | H2_2024  |            5.88991 |       1.09631  |           -6.86285 |
| trend_following_MA20_100 | 15m         | H2_2024  |            4.27188 |       0.495241 |           -5.79693 |

## Top 10 Configurations (by Sharpe Ratio)

| strategy                 | timeframe   | period   |   sharpe_ratio |   total_return_pct |   max_drawdown_pct |
|:-------------------------|:------------|:---------|---------------:|-------------------:|-------------------:|
| trend_following_MA21_55  | 1h          | H2_2024  |       2.62152  |           14.7237  |           -4.2763  |
| trend_following_MA20_100 | 1h          | H2_2024  |       2.53418  |           14.249   |           -5.57991 |
| trend_following_MA20_50  | 1h          | H2_2024  |       2.39593  |           13.2991  |           -5.10947 |
| trend_following_MA9_21   | 1h          | H2_2024  |       2.22942  |           12.1912  |           -5.73537 |
| trend_following_MA10_30  | 1h          | H2_2024  |       2.21824  |           12.0762  |           -5.83287 |
| auto_MA20_100            | 1h          | H2_2024  |       1.38839  |            7.83307 |           -7.67334 |
| auto_MA10_30             | 1h          | H2_2024  |       1.09631  |            5.88991 |           -6.86285 |
| trend_following_MA20_50  | 15m         | H2_2024  |       0.929947 |            9.23801 |           -5.18502 |
| trend_following_MA21_55  | 15m         | H2_2024  |       0.821162 |            7.82751 |           -4.78528 |
| trend_following_MA20_100 | 15m         | H2_2024  |       0.495241 |            4.27188 |           -5.79693 |

## Average Performance by Base Strategy

| base_strategy   |   total_return_pct |   max_drawdown_pct |   sharpe_ratio |   win_rate_pct |
|:----------------|-------------------:|-------------------:|---------------:|---------------:|
| auto            |             -27.86 |             -31.06 |          -2.47 |          31.03 |
| trend_following |              -1.36 |             -10.27 |           0.28 |          36.38 |

## Conclusion
The **21/55 (Fibonacci)** MA pair on the **1h** timeframe showed the best risk-adjusted performance (+14.72% return, 2.62 Sharpe) during the H2 2024 period and was selected for the live deployment.
