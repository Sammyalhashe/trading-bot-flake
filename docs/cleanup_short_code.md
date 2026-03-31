# SHORT Code Cleanup Checklist

## Current Status

Shorting is **DISABLED** but legacy code remains in multiple places.

## Files with SHORT References

### trading_bot.py
- Line 105: `ENABLE_SHORT = config.enable_short` (unused variable)
- Line 109: `SHORT_RISK_PERCENTAGE` (unused variable)
- Line 297: Check for `:SHORT` suffix in state keys
- Line 330-334: Clean up short entries from state
- Line 344: Remove orphaned `:SHORT` entries
- Line 808: Skip `:SHORT` keys when caching

### config/trading_config.py
- `enable_short: bool` field (always False now)
- `short_risk_pct: str` field (unused)

### strategies/
- `enables_short` property in both strategies (always False)
- `scan_short_entry()` methods (returns None)
- `rank_short_candidates()` methods (returns [])

## Cleanup Actions

### Option 1: Complete Removal (Recommended)
Remove ALL short-related code since it's not being used:

1. **Remove from TradingConfig:**
   - `enable_short`
   - `short_risk_pct`

2. **Remove from trading_bot.py:**
   - `ENABLE_SHORT` variable
   - `SHORT_RISK_PERCENTAGE` variable
   - All `:SHORT` key filtering (keep the filtering logic but remove short-specific branches)

3. **Remove from strategies:**
   - `enables_short` property
   - `scan_short_entry()` method
   - `rank_short_candidates()` method

4. **Remove from tests:**
   - Any short-related test cases

### Option 2: Keep for Future Use
If you might re-enable shorting later:

1. **Add clear documentation** that short selling is disabled
2. **Add runtime check** that errors if someone tries to enable it:
   ```python
   if config.enable_short:
       raise ValueError("Short selling is not currently supported")
   ```
3. **Move to separate module** - `strategies/short_selling.py` (disabled)

## Recommended Approach

**Go with Option 1** - Complete removal because:
- ✅ Simplifies codebase
- ✅ Removes confusion
- ✅ Easier to maintain
- ✅ Can always add back from git history if needed
- ✅ Shorting crypto is risky anyway (unlimited loss potential)

## Implementation

I can create a cleanup script that:
1. Removes all SHORT-related code
2. Updates tests
3. Ensures nothing breaks
4. Creates a commit with the changes

Would you like me to proceed with the cleanup?
