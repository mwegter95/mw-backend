/**
 * Mock data layer — mirrors the seeded NestJS/Postgres backend exactly.
 * Used when the real API is unreachable (502/network). All arithmetic
 * uses the same integer-only money functions as the backend.
 */

import { calcAdvance, calcFee, calcReserve } from './money';
import type { Invoice, Account, LedgerEntry } from './api';
import { getUser } from './auth';

// ---- Seed users (mirror seed.ts) ----
interface MockUser {
  id: number; email: string; password: string;
  name: string; role: 'admin' | 'underwriter' | 'carrier' | 'driver';
}
const SEED_USERS: MockUser[] = [
  { id: 1, email: 'admin@factoringdemo.com',       password: 'Admin@12345',   name: 'Alex Rivera',    role: 'admin' },
  { id: 2, email: 'underwriter@factoringdemo.com', password: 'Under@12345',   name: 'Jordan Lee',     role: 'underwriter' },
  { id: 3, email: 'carrier@factoringdemo.com',     password: 'Carrier@12345', name: 'Sam Trucking LLC', role: 'carrier' },
];

// ---- Pre-compute approved/disbursed/collected invoice fields ----
// INV-002 approved
const i2f = 875000, i2ar = 8500, i2fr = 300;
const i2adv = calcAdvance(i2f, i2ar);   // 743750
const i2fee = calcFee(i2f, i2fr);       // 26250
const i2res = calcReserve(i2f, i2adv);  // 131250

// INV-003 disbursed
const i3f = 2300000, i3ar = 9200, i3fr = 150;
const i3adv = calcAdvance(i3f, i3ar);   // 2116000
const i3fee = calcFee(i3f, i3fr);       // 34500
const i3res = calcReserve(i3f, i3adv);  // 184000

// INV-005 collected
const i5f = 1875000, i5ar = 9000, i5fr = 200;
const i5adv = calcAdvance(i5f, i5ar);   // 1687500
const i5fee = calcFee(i5f, i5fr);       // 37500
const i5res = calcReserve(i5f, i5adv);  // 187500

// ---- Seeded invoices ----
const SEED_INVOICES: Invoice[] = [
  { id:1, invoiceNumber:'INV-001', carrierId:3, underwriterId:null, payerName:'Swift Logistics Inc.', payerDaysToPay:30, faceValueMinorUnits:1250000, advanceRateBps:9000, feeRateBps:200, advanceMinorUnits:null, feeMinorUnits:null, reserveMinorUnits:null, currency:'USD', status:'pending', notes:null, submittedAt:'2026-06-14T10:00:00.000Z', approvedAt:null, disbursedAt:null, collectedAt:null },
  { id:2, invoiceNumber:'INV-002', carrierId:3, underwriterId:2, payerName:'Northern Freight Co.', payerDaysToPay:45, faceValueMinorUnits:i2f, advanceRateBps:i2ar, feeRateBps:i2fr, advanceMinorUnits:i2adv, feeMinorUnits:i2fee, reserveMinorUnits:i2res, currency:'USD', status:'approved', notes:null, submittedAt:'2026-06-13T09:00:00.000Z', approvedAt:'2026-06-13T14:00:00.000Z', disbursedAt:null, collectedAt:null },
  { id:3, invoiceNumber:'INV-003', carrierId:3, underwriterId:2, payerName:'Prime Haul Partners', payerDaysToPay:21, faceValueMinorUnits:i3f, advanceRateBps:i3ar, feeRateBps:i3fr, advanceMinorUnits:i3adv, feeMinorUnits:i3fee, reserveMinorUnits:i3res, currency:'USD', status:'disbursed', notes:null, submittedAt:'2026-06-12T08:00:00.000Z', approvedAt:'2026-06-12T11:00:00.000Z', disbursedAt:'2026-06-12T15:00:00.000Z', collectedAt:null },
  { id:4, invoiceNumber:'INV-004', carrierId:3, underwriterId:2, payerName:'Westbound Cargo LLC', payerDaysToPay:60, faceValueMinorUnits:560000, advanceRateBps:8000, feeRateBps:400, advanceMinorUnits:null, feeMinorUnits:null, reserveMinorUnits:null, currency:'USD', status:'rejected', notes:'Payer credit score below threshold', submittedAt:'2026-06-11T07:00:00.000Z', approvedAt:null, disbursedAt:null, collectedAt:null },
  { id:5, invoiceNumber:'INV-005', carrierId:3, underwriterId:2, payerName:'Eagle Transport', payerDaysToPay:30, faceValueMinorUnits:i5f, advanceRateBps:i5ar, feeRateBps:i5fr, advanceMinorUnits:i5adv, feeMinorUnits:i5fee, reserveMinorUnits:i5res, currency:'USD', status:'collected', notes:null, submittedAt:'2026-06-10T06:00:00.000Z', approvedAt:'2026-06-10T09:00:00.000Z', disbursedAt:'2026-06-10T12:00:00.000Z', collectedAt:'2026-06-11T10:00:00.000Z' },
];

// Account balances after all seeded transactions:
// INV-003 disburse: DR FACTORING_REC +2116000, CR CASH_BANK -2116000
// INV-005 disburse: DR FACTORING_REC +1687500, CR CASH_BANK -1687500
// INV-005 collect:  DR CASH_BANK +1875000, CR FACTORING_REC -1687500, CR FEE_REVENUE +37500
// Net: CASH_BANK = 5000000000 - 2116000 - 1687500 + 1875000 = 4998071500
//      FACTORING_REC = 2116000 + 1687500 - 1687500 = 2116000 (only INV-003 open)
//      FEE_REVENUE = 37500
const SEED_ACCOUNTS: Account[] = [
  { id:1, name:'Factoring Receivable',    code:'FACTORING_REC',  type:'asset',     currency:'USD', balanceMinorUnits:2116000,    updatedAt:'2026-06-12T15:00:00.000Z' },
  { id:2, name:'Cash / Bank',             code:'CASH_BANK',      type:'asset',     currency:'USD', balanceMinorUnits:4998071500, updatedAt:'2026-06-11T10:00:00.000Z' },
  { id:3, name:'Fee Revenue',             code:'FEE_REVENUE',    type:'revenue',   currency:'USD', balanceMinorUnits:37500,      updatedAt:'2026-06-11T10:00:00.000Z' },
  { id:4, name:'Carrier Reserve Holdback',code:'CARRIER_RESERVE',type:'liability', currency:'USD', balanceMinorUnits:0,          updatedAt:'2026-06-10T00:00:00.000Z' },
];

const SEED_LEDGER: LedgerEntry[] = [
  // INV-005 collected (most recent, shown first)
  { id:5, invoiceId:5, accountId:2, entryType:'debit',  amountMinorUnits:1875000, currency:'USD', description:'Payment received - INV-005 Eagle Transport (face value)', postedAt:'2026-06-11T10:00:00.000Z' },
  { id:6, invoiceId:5, accountId:1, entryType:'credit', amountMinorUnits:1687500, currency:'USD', description:'Receivable closed - INV-005',                            postedAt:'2026-06-11T10:00:00.000Z' },
  { id:7, invoiceId:5, accountId:3, entryType:'credit', amountMinorUnits:37500,   currency:'USD', description:'Fee revenue recognized - INV-005',                       postedAt:'2026-06-11T10:00:00.000Z' },
  // INV-005 disbursed
  { id:3, invoiceId:5, accountId:1, entryType:'debit',  amountMinorUnits:1687500, currency:'USD', description:'Advance disbursed - INV-005 Eagle Transport',           postedAt:'2026-06-10T12:00:00.000Z' },
  { id:4, invoiceId:5, accountId:2, entryType:'credit', amountMinorUnits:1687500, currency:'USD', description:'Cash out - INV-005 advance to carrier',                 postedAt:'2026-06-10T12:00:00.000Z' },
  // INV-003 disbursed
  { id:1, invoiceId:3, accountId:1, entryType:'debit',  amountMinorUnits:2116000, currency:'USD', description:'Advance disbursed - INV-003 Prime Haul Partners',       postedAt:'2026-06-12T15:00:00.000Z' },
  { id:2, invoiceId:3, accountId:2, entryType:'credit', amountMinorUnits:2116000, currency:'USD', description:'Cash out - INV-003 advance to carrier',                 postedAt:'2026-06-12T15:00:00.000Z' },
];

// ---- Session state ----
const STATE_KEY = 'factoring_mock_state';

interface MockState {
  invoices: Invoice[];
  accounts: Account[];
  ledger: LedgerEntry[];
  nextInvoiceId: number;
  nextLedgerId: number;
}

function deepClone<T>(obj: T): T {
  return JSON.parse(JSON.stringify(obj)) as T;
}

function getState(): MockState {
  if (typeof window === 'undefined') {
    return { invoices: deepClone(SEED_INVOICES), accounts: deepClone(SEED_ACCOUNTS), ledger: deepClone(SEED_LEDGER), nextInvoiceId: 6, nextLedgerId: 8 };
  }
  try {
    const raw = sessionStorage.getItem(STATE_KEY);
    if (raw) return JSON.parse(raw) as MockState;
  } catch { /* ignore */ }
  // Initialize fresh state
  const fresh: MockState = { invoices: deepClone(SEED_INVOICES), accounts: deepClone(SEED_ACCOUNTS), ledger: deepClone(SEED_LEDGER), nextInvoiceId: 6, nextLedgerId: 8 };
  saveState(fresh);
  return fresh;
}

function saveState(state: MockState): void {
  if (typeof window !== 'undefined') {
    try { sessionStorage.setItem(STATE_KEY, JSON.stringify(state)); } catch { /* ignore */ }
  }
}

// ---- Mock operations ----

export function mockLogin(email: string, password: string): { access_token: string; user: { id: number; email: string; role: string; name: string } } {
  const user = SEED_USERS.find(u => u.email === email && u.password === password);
  if (!user) throw new Error('Invalid email or password');
  // Fake token: header.payload.sig (frontend only checks localStorage, never verifies sig)
  const payload = typeof window !== 'undefined' ? btoa(JSON.stringify({ sub: user.id, email: user.email, role: user.role })) : 'mock';
  return {
    access_token: `mock.${payload}.sig`,
    user: { id: user.id, email: user.email, role: user.role, name: user.name },
  };
}

export function mockMe(): { sub: number; email: string; role: string; name: string } {
  const u = getUser();
  if (!u) throw new Error('Unauthorized');
  return { sub: u.sub, email: u.email, role: u.role, name: u.name };
}

export function mockListInvoices(): Invoice[] {
  const u = getUser();
  const state = getState();
  if (u?.role === 'carrier') return state.invoices.filter(i => i.carrierId === u.sub);
  return [...state.invoices].reverse(); // newest first
}

export function mockGetInvoice(id: number): Invoice {
  const inv = getState().invoices.find(i => i.id === id);
  if (!inv) throw new Error('Invoice not found');
  return inv;
}

export function mockCreateInvoice(data: Record<string, unknown>): Invoice {
  const u = getUser();
  const state = getState();
  const id = state.nextInvoiceId++;
  const face = data.faceValueMinorUnits as number;
  const inv: Invoice = {
    id,
    invoiceNumber: `INV-${String(id).padStart(3, '0')}`,
    carrierId: u?.sub ?? 3,
    underwriterId: null,
    payerName: data.payerName as string,
    payerDaysToPay: (data.payerDaysToPay as number) ?? 30,
    faceValueMinorUnits: face,
    advanceRateBps: data.advanceRateBps as number,
    feeRateBps: data.feeRateBps as number,
    advanceMinorUnits: null,
    feeMinorUnits: null,
    reserveMinorUnits: null,
    currency: (data.currency as 'USD' | 'BRL') ?? 'USD',
    status: 'pending',
    notes: (data.notes as string) ?? null,
    submittedAt: new Date().toISOString(),
    approvedAt: null,
    disbursedAt: null,
    collectedAt: null,
  };
  state.invoices.push(inv);
  saveState(state);
  return inv;
}

export function mockApproveInvoice(id: number, notes?: string): Invoice {
  const state = getState();
  const inv = state.invoices.find(i => i.id === id);
  if (!inv) throw new Error('Invoice not found');
  if (inv.status !== 'pending') throw new Error(`Cannot approve invoice with status: ${inv.status}`);
  const u = getUser();
  inv.advanceMinorUnits = calcAdvance(inv.faceValueMinorUnits, inv.advanceRateBps);
  inv.feeMinorUnits = calcFee(inv.faceValueMinorUnits, inv.feeRateBps);
  inv.reserveMinorUnits = calcReserve(inv.faceValueMinorUnits, inv.advanceMinorUnits);
  inv.underwriterId = u?.sub ?? 2;
  inv.status = 'approved';
  inv.approvedAt = new Date().toISOString();
  if (notes) inv.notes = notes;
  saveState(state);
  return inv;
}

export function mockRejectInvoice(id: number, notes?: string): Invoice {
  const state = getState();
  const inv = state.invoices.find(i => i.id === id);
  if (!inv) throw new Error('Invoice not found');
  if (inv.status !== 'pending') throw new Error(`Cannot reject invoice with status: ${inv.status}`);
  const u = getUser();
  inv.underwriterId = u?.sub ?? 2;
  inv.status = 'rejected';
  inv.approvedAt = new Date().toISOString();
  if (notes) inv.notes = notes;
  saveState(state);
  return inv;
}

export function mockDisburseInvoice(id: number): Invoice {
  const state = getState();
  const inv = state.invoices.find(i => i.id === id);
  if (!inv) throw new Error('Invoice not found');
  if (inv.status !== 'approved' || inv.advanceMinorUnits == null) throw new Error(`Cannot disburse invoice with status: ${inv.status}`);
  const adv = inv.advanceMinorUnits;
  const now = new Date().toISOString();
  inv.status = 'disbursed';
  inv.disbursedAt = now;

  // Double-entry: DR FACTORING_REC, CR CASH_BANK
  const factRec = state.accounts.find(a => a.code === 'FACTORING_REC')!;
  const cashBank = state.accounts.find(a => a.code === 'CASH_BANK')!;
  factRec.balanceMinorUnits += adv;
  cashBank.balanceMinorUnits -= adv;
  factRec.updatedAt = now;
  cashBank.updatedAt = now;

  const l1 = state.nextLedgerId++;
  const l2 = state.nextLedgerId++;
  state.ledger.unshift({ id: l1, invoiceId: id, accountId: factRec.id, entryType: 'debit',  amountMinorUnits: adv, currency: inv.currency, description: `Advance disbursed - ${inv.invoiceNumber} ${inv.payerName}`, postedAt: now });
  state.ledger.unshift({ id: l2, invoiceId: id, accountId: cashBank.id, entryType: 'credit', amountMinorUnits: adv, currency: inv.currency, description: `Cash out - ${inv.invoiceNumber} advance to carrier`, postedAt: now });
  saveState(state);
  return inv;
}

export function mockCollectInvoice(id: number): Invoice {
  const state = getState();
  const inv = state.invoices.find(i => i.id === id);
  if (!inv) throw new Error('Invoice not found');
  if (inv.status !== 'disbursed' || inv.advanceMinorUnits == null || inv.feeMinorUnits == null) throw new Error(`Cannot collect invoice with status: ${inv.status}`);
  const face = inv.faceValueMinorUnits;
  const adv = inv.advanceMinorUnits;
  const fee = inv.feeMinorUnits;
  const now = new Date().toISOString();
  inv.status = 'collected';
  inv.collectedAt = now;

  // DR CASH_BANK +face, CR FACTORING_REC -adv, CR FEE_REVENUE +fee
  const factRec = state.accounts.find(a => a.code === 'FACTORING_REC')!;
  const cashBank = state.accounts.find(a => a.code === 'CASH_BANK')!;
  const feeRev = state.accounts.find(a => a.code === 'FEE_REVENUE')!;
  cashBank.balanceMinorUnits += face;
  factRec.balanceMinorUnits -= adv;
  feeRev.balanceMinorUnits += fee;
  cashBank.updatedAt = now;
  factRec.updatedAt = now;
  feeRev.updatedAt = now;

  const l1 = state.nextLedgerId++;
  const l2 = state.nextLedgerId++;
  const l3 = state.nextLedgerId++;
  state.ledger.unshift({ id: l1, invoiceId: id, accountId: cashBank.id, entryType: 'debit',  amountMinorUnits: face, currency: inv.currency, description: `Payment received - ${inv.invoiceNumber} (face value)`, postedAt: now });
  state.ledger.unshift({ id: l2, invoiceId: id, accountId: factRec.id, entryType: 'credit', amountMinorUnits: adv,  currency: inv.currency, description: `Receivable closed - ${inv.invoiceNumber}`, postedAt: now });
  state.ledger.unshift({ id: l3, invoiceId: id, accountId: feeRev.id,  entryType: 'credit', amountMinorUnits: fee,  currency: inv.currency, description: `Fee revenue recognized - ${inv.invoiceNumber}`, postedAt: now });
  saveState(state);
  return inv;
}

export function mockListAccounts(): Account[] {
  return getState().accounts;
}

export function mockGetAccountEntries(id: number): { account: Account; entries: LedgerEntry[] } {
  const state = getState();
  const account = state.accounts.find(a => a.id === id);
  if (!account) throw new Error('Account not found');
  const entries = state.ledger.filter(e => e.accountId === id).sort((a, b) => b.postedAt.localeCompare(a.postedAt));
  return { account, entries };
}

export function mockRecentLedger(): LedgerEntry[] {
  return [...getState().ledger].sort((a, b) => b.postedAt.localeCompare(a.postedAt)).slice(0, 50);
}

// ---- Mock mode flag (sessionStorage) ----
const MOCK_KEY = 'factoring_mock_active';

export function isMockMode(): boolean {
  if (typeof window === 'undefined') return false;
  return sessionStorage.getItem(MOCK_KEY) === '1';
}

export function enableMockMode(): void {
  if (typeof window !== 'undefined') {
    sessionStorage.setItem(MOCK_KEY, '1');
    // Ensure session state is initialized
    getState();
  }
}

// ---- Central mock request router ----
export function mockRequest<T>(path: string, options: RequestInit = {}): T {
  const method = ((options.method ?? 'GET') as string).toUpperCase();
  let body: Record<string, unknown> = {};
  if (options.body && typeof options.body === 'string') {
    try { body = JSON.parse(options.body); } catch { /* empty body ok */ }
  }

  // Auth
  if (path === '/auth/login' && method === 'POST') return mockLogin(body.email as string, body.password as string) as T;
  if (path === '/auth/me' && method === 'GET') return mockMe() as T;

  // Invoices
  if (path === '/invoices' && method === 'GET') return mockListInvoices() as T;
  if (path === '/invoices' && method === 'POST') return mockCreateInvoice(body) as T;

  const invIdMatch = path.match(/^\/invoices\/(\d+)$/);
  if (invIdMatch && method === 'GET') return mockGetInvoice(parseInt(invIdMatch[1])) as T;

  const approveMatch = path.match(/^\/invoices\/(\d+)\/approve$/);
  if (approveMatch && method === 'PATCH') return mockApproveInvoice(parseInt(approveMatch[1]), body.notes as string | undefined) as T;

  const rejectMatch = path.match(/^\/invoices\/(\d+)\/reject$/);
  if (rejectMatch && method === 'PATCH') return mockRejectInvoice(parseInt(rejectMatch[1]), body.notes as string | undefined) as T;

  const disburseMatch = path.match(/^\/invoices\/(\d+)\/disburse$/);
  if (disburseMatch && method === 'PATCH') return mockDisburseInvoice(parseInt(disburseMatch[1])) as T;

  const collectMatch = path.match(/^\/invoices\/(\d+)\/collect$/);
  if (collectMatch && method === 'PATCH') return mockCollectInvoice(parseInt(collectMatch[1])) as T;

  // Ledger / Accounts
  if (path === '/accounts' && method === 'GET') return mockListAccounts() as T;
  if (path === '/ledger' && method === 'GET') return mockRecentLedger() as T;

  const entriesMatch = path.match(/^\/accounts\/(\d+)\/entries$/);
  if (entriesMatch && method === 'GET') return mockGetAccountEntries(parseInt(entriesMatch[1])) as T;

  throw new Error(`Mock: unhandled ${method} ${path}`);
}
