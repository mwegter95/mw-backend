export type Currency = 'USD' | 'BRL';

export interface Money {
  currency: Currency;
  minorUnits: number;
}

// Integer arithmetic only — no floats on money paths
export function calcAdvance(faceValueMinorUnits: number, advanceRateBps: number): number {
  return Math.floor((faceValueMinorUnits * advanceRateBps) / 10000);
}

export function calcFee(faceValueMinorUnits: number, feeRateBps: number): number {
  return Math.floor((faceValueMinorUnits * feeRateBps) / 10000);
}

export function calcReserve(faceValueMinorUnits: number, advanceMinorUnits: number): number {
  return faceValueMinorUnits - advanceMinorUnits;
}

export function formatMoney(minorUnits: number, currency: Currency = 'USD'): string {
  const symbols: Record<Currency, string> = { USD: '$', BRL: 'R$' };
  const major = (minorUnits / 100).toFixed(2);
  return `${symbols[currency]}${Number(major).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
}

export function parseDollarsToMinorUnits(dollarStr: string): number {
  const cleaned = dollarStr.replace(/[^0-9.]/g, '');
  const float = parseFloat(cleaned);
  if (isNaN(float)) return 0;
  return Math.round(float * 100);
}
