'use client';

import { formatMoney, type Currency } from '../lib/money';

interface MoneyDisplayProps {
  minorUnits: number;
  currency?: Currency;
  size?: 'sm' | 'md' | 'lg' | 'xl';
  className?: string;
}

const sizes: Record<string, string> = {
  sm: '0.8rem',
  md: '1rem',
  lg: '1.375rem',
  xl: '1.875rem',
};

export function MoneyDisplay({
  minorUnits,
  currency = 'USD',
  size = 'md',
  className,
}: MoneyDisplayProps) {
  return (
    <span
      className={className}
      style={{
        fontFamily: 'var(--font-mono)',
        fontVariantNumeric: 'tabular-nums',
        fontSize: sizes[size],
        fontWeight: 600,
        color: minorUnits < 0 ? 'var(--status-rejected)' : undefined,
        letterSpacing: '-0.02em',
      }}
    >
      {formatMoney(minorUnits, currency)}
    </span>
  );
}
