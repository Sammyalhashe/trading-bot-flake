#!/usr/bin/env python3
"""Download historical OHLCV data from Coinbase Public API."""
import requests
import pandas as pd
import time
from datetime import datetime, timedelta
import argparse
from pathlib import Path
import urllib3

# Disable SSL warnings (for corporate environments)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def download_coinbase_candles(product_id, start_date, end_date, granularity=3600):
    """
    Download historical candles from Coinbase Public API.

    Args:
        product_id: e.g., 'BTC-USD', 'ETH-USD'
        start_date: datetime object
        end_date: datetime object
        granularity: seconds per candle (60=1m, 300=5m, 900=15m, 3600=1h, 21600=6h, 86400=1d)

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
    """
    url = f"https://api.exchange.coinbase.com/products/{product_id}/candles"

    # Coinbase limits to 300 candles per request
    max_candles = 300
    period_seconds = max_candles * granularity

    all_data = []
    current_start = start_date

    while current_start < end_date:
        current_end = min(current_start + timedelta(seconds=period_seconds), end_date)

        params = {
            'start': current_start.isoformat(),
            'end': current_end.isoformat(),
            'granularity': granularity
        }

        print(f"Fetching {product_id} from {current_start.date()} to {current_end.date()}...")

        try:
            response = requests.get(url, params=params, timeout=10, verify=False)
            response.raise_for_status()
            data = response.json()

            if isinstance(data, dict) and 'message' in data:
                print(f"Error: {data['message']}")
                break

            if data:
                all_data.extend(data)
                print(f"  Retrieved {len(data)} candles")

            # Rate limiting: sleep to avoid hitting API limits
            time.sleep(0.5)

        except requests.exceptions.RequestException as e:
            print(f"Request failed: {e}")
            break

        current_start = current_end

    if not all_data:
        print(f"No data retrieved for {product_id}")
        return None

    # Convert to DataFrame
    # Coinbase returns: [timestamp, low, high, open, close, volume]
    df = pd.DataFrame(all_data, columns=['timestamp', 'low', 'high', 'open', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
    df = df.sort_values('timestamp').reset_index(drop=True)

    # Reorder to standard OHLCV format
    df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]

    return df


def main():
    parser = argparse.ArgumentParser(description='Download historical crypto data from Coinbase')
    parser.add_argument('--symbols', nargs='+', default=['BTC-USD', 'ETH-USD'],
                        help='Product IDs to download (default: BTC-USD ETH-USD)')
    parser.add_argument('--start', type=str, default='2023-01-01',
                        help='Start date (YYYY-MM-DD, default: 2023-01-01)')
    parser.add_argument('--end', type=str, default=None,
                        help='End date (YYYY-MM-DD, default: today)')
    parser.add_argument('--granularity', type=int, default=3600,
                        help='Candle size in seconds (default: 3600 = 1 hour)')
    parser.add_argument('--output-dir', type=str, default='../data/historical',
                        help='Output directory (default: ../data/historical)')

    args = parser.parse_args()

    # Parse dates
    start_date = datetime.fromisoformat(args.start)
    end_date = datetime.fromisoformat(args.end) if args.end else datetime.now()

    # Create output directory
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Granularity mapping for filename
    granularity_map = {
        60: '1m',
        300: '5m',
        900: '15m',
        3600: '1h',
        21600: '6h',
        86400: '1d'
    }
    timeframe = granularity_map.get(args.granularity, f'{args.granularity}s')

    print(f"Downloading data from {start_date.date()} to {end_date.date()}")
    print(f"Granularity: {timeframe} ({args.granularity} seconds)")
    print(f"Symbols: {', '.join(args.symbols)}\n")

    for symbol in args.symbols:
        print(f"\n{'='*60}")
        print(f"Downloading {symbol}")
        print(f"{'='*60}")

        df = download_coinbase_candles(symbol, start_date, end_date, args.granularity)

        if df is not None and len(df) > 0:
            # Save to CSV
            filename = f"{symbol.replace('-', '_')}_{timeframe}_{start_date.date()}_{end_date.date()}.csv"
            filepath = output_dir / filename
            df.to_csv(filepath, index=False)

            print(f"\n✓ Saved {len(df)} candles to {filepath}")
            print(f"  Date range: {df['timestamp'].min()} to {df['timestamp'].max()}")
            print(f"  File size: {filepath.stat().st_size / 1024:.1f} KB")
        else:
            print(f"\n✗ No data retrieved for {symbol}")

    print(f"\n{'='*60}")
    print(f"Download complete! Data saved to {output_dir}/")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
