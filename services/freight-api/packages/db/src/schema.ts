import {
  pgTable,
  serial,
  text,
  integer,
  timestamp,
  pgEnum,
} from 'drizzle-orm/pg-core';

export const roleEnum = pgEnum('role', ['admin', 'underwriter', 'carrier', 'driver']);
export const currencyEnum = pgEnum('currency', ['USD', 'BRL']);
export const invoiceStatusEnum = pgEnum('invoice_status', [
  'pending',
  'approved',
  'rejected',
  'disbursed',
  'collected',
]);
export const entryTypeEnum = pgEnum('entry_type', ['debit', 'credit']);
export const accountTypeEnum = pgEnum('account_type', [
  'asset',
  'liability',
  'revenue',
  'expense',
]);

export const users = pgTable('users', {
  id: serial('id').primaryKey(),
  email: text('email').notNull().unique(),
  passwordHash: text('password_hash').notNull(),
  name: text('name').notNull(),
  role: roleEnum('role').notNull().default('carrier'),
  createdAt: timestamp('created_at').defaultNow().notNull(),
});

export const accounts = pgTable('accounts', {
  id: serial('id').primaryKey(),
  name: text('name').notNull(),
  code: text('code').notNull().unique(),
  type: accountTypeEnum('type').notNull(),
  currency: currencyEnum('currency').notNull().default('USD'),
  balanceMinorUnits: integer('balance_minor_units').notNull().default(0),
  updatedAt: timestamp('updated_at').defaultNow().notNull(),
});

export const invoices = pgTable('invoices', {
  id: serial('id').primaryKey(),
  invoiceNumber: text('invoice_number').notNull().unique(),
  carrierId: integer('carrier_id')
    .notNull()
    .references(() => users.id),
  underwriterId: integer('underwriter_id').references(() => users.id),
  payerName: text('payer_name').notNull(),
  payerDaysToPay: integer('payer_days_to_pay').notNull().default(30),
  faceValueMinorUnits: integer('face_value_minor_units').notNull(),
  advanceRateBps: integer('advance_rate_bps').notNull(),
  feeRateBps: integer('fee_rate_bps').notNull(),
  advanceMinorUnits: integer('advance_minor_units'),
  feeMinorUnits: integer('fee_minor_units'),
  reserveMinorUnits: integer('reserve_minor_units'),
  currency: currencyEnum('currency').notNull().default('USD'),
  status: invoiceStatusEnum('status').notNull().default('pending'),
  notes: text('notes'),
  submittedAt: timestamp('submitted_at').defaultNow().notNull(),
  approvedAt: timestamp('approved_at'),
  disbursedAt: timestamp('disbursed_at'),
  collectedAt: timestamp('collected_at'),
});

export const ledgerEntries = pgTable('ledger_entries', {
  id: serial('id').primaryKey(),
  invoiceId: integer('invoice_id').references(() => invoices.id),
  accountId: integer('account_id')
    .notNull()
    .references(() => accounts.id),
  entryType: entryTypeEnum('entry_type').notNull(),
  amountMinorUnits: integer('amount_minor_units').notNull(),
  currency: currencyEnum('currency').notNull().default('USD'),
  description: text('description').notNull(),
  postedAt: timestamp('posted_at').defaultNow().notNull(),
});
