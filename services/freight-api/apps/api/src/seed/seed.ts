/**
 * Seed script — run once against a fresh DB.
 * npx ts-node src/seed/seed.ts
 *
 * Credentials seeded:
 *   admin@factoringdemo.com       / Admin@12345
 *   underwriter@factoringdemo.com / Under@12345
 *   carrier@factoringdemo.com     / Carrier@12345
 */
import 'reflect-metadata';
import { drizzle } from 'drizzle-orm/postgres-js';
import postgres from 'postgres';
import * as bcrypt from 'bcryptjs';
import {
  users,
  accounts,
  invoices,
} from '../../../../packages/db/src/schema';

const SALT = 10;

async function main() {
  const client = postgres(process.env.DATABASE_URL!, { prepare: false });
  const db = drizzle(client);

  // --- Create tables (raw SQL for portability) ---
  await client`
    CREATE TYPE IF NOT EXISTS role AS ENUM ('admin','underwriter','carrier','driver');
    CREATE TYPE IF NOT EXISTS currency AS ENUM ('USD','BRL');
    CREATE TYPE IF NOT EXISTS invoice_status AS ENUM ('pending','approved','rejected','disbursed','collected');
    CREATE TYPE IF NOT EXISTS entry_type AS ENUM ('debit','credit');
    CREATE TYPE IF NOT EXISTS account_type AS ENUM ('asset','liability','revenue','expense');

    CREATE TABLE IF NOT EXISTS users (
      id SERIAL PRIMARY KEY,
      email TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      name TEXT NOT NULL,
      role role NOT NULL DEFAULT 'carrier',
      created_at TIMESTAMP NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS accounts (
      id SERIAL PRIMARY KEY,
      name TEXT NOT NULL,
      code TEXT NOT NULL UNIQUE,
      type account_type NOT NULL,
      currency currency NOT NULL DEFAULT 'USD',
      balance_minor_units INTEGER NOT NULL DEFAULT 0,
      updated_at TIMESTAMP NOT NULL DEFAULT NOW()
    );

    CREATE TABLE IF NOT EXISTS invoices (
      id SERIAL PRIMARY KEY,
      invoice_number TEXT NOT NULL UNIQUE,
      carrier_id INTEGER NOT NULL REFERENCES users(id),
      underwriter_id INTEGER REFERENCES users(id),
      payer_name TEXT NOT NULL,
      payer_days_to_pay INTEGER NOT NULL DEFAULT 30,
      face_value_minor_units INTEGER NOT NULL,
      advance_rate_bps INTEGER NOT NULL,
      fee_rate_bps INTEGER NOT NULL,
      advance_minor_units INTEGER,
      fee_minor_units INTEGER,
      reserve_minor_units INTEGER,
      currency currency NOT NULL DEFAULT 'USD',
      status invoice_status NOT NULL DEFAULT 'pending',
      notes TEXT,
      submitted_at TIMESTAMP NOT NULL DEFAULT NOW(),
      approved_at TIMESTAMP,
      disbursed_at TIMESTAMP,
      collected_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS ledger_entries (
      id SERIAL PRIMARY KEY,
      invoice_id INTEGER REFERENCES invoices(id),
      account_id INTEGER NOT NULL REFERENCES accounts(id),
      entry_type entry_type NOT NULL,
      amount_minor_units INTEGER NOT NULL,
      currency currency NOT NULL DEFAULT 'USD',
      description TEXT NOT NULL,
      posted_at TIMESTAMP NOT NULL DEFAULT NOW()
    );
  `;

  // --- Users ---
  const [admin, underwriter, carrier] = await db
    .insert(users)
    .values([
      {
        email: 'admin@factoringdemo.com',
        passwordHash: await bcrypt.hash('Admin@12345', SALT),
        name: 'Alex Rivera',
        role: 'admin',
      },
      {
        email: 'underwriter@factoringdemo.com',
        passwordHash: await bcrypt.hash('Under@12345', SALT),
        name: 'Jordan Lee',
        role: 'underwriter',
      },
      {
        email: 'carrier@factoringdemo.com',
        passwordHash: await bcrypt.hash('Carrier@12345', SALT),
        name: 'Sam Trucking LLC',
        role: 'carrier',
      },
    ])
    .onConflictDoNothing()
    .returning();

  console.log('Users seeded:', [admin, underwriter, carrier].filter(Boolean).map(u => u?.email));

  // Fetch user IDs (in case they already exist)
  const allUsers = await client`SELECT id, email FROM users WHERE email IN ('admin@factoringdemo.com','underwriter@factoringdemo.com','carrier@factoringdemo.com')`;
  const userMap: Record<string, number> = {};
  for (const u of allUsers) userMap[u.email as string] = u.id as number;

  // --- Accounts ---
  await db
    .insert(accounts)
    .values([
      { name: 'Factoring Receivable', code: 'FACTORING_REC', type: 'asset', currency: 'USD', balanceMinorUnits: 0 },
      { name: 'Cash / Bank', code: 'CASH_BANK', type: 'asset', currency: 'USD', balanceMinorUnits: 5000000000 },
      { name: 'Fee Revenue', code: 'FEE_REVENUE', type: 'revenue', currency: 'USD', balanceMinorUnits: 0 },
      { name: 'Carrier Reserve Holdback', code: 'CARRIER_RESERVE', type: 'liability', currency: 'USD', balanceMinorUnits: 0 },
    ])
    .onConflictDoNothing();

  console.log('Accounts seeded');

  // --- Invoices ---
  const carrierId = userMap['carrier@factoringdemo.com'];
  const underwriterId = userMap['underwriter@factoringdemo.com'];
  const adminId = userMap['admin@factoringdemo.com'];

  if (!carrierId) {
    console.error('carrier user not found, aborting invoices seed');
    process.exit(1);
  }

  const now = new Date();

  await db
    .insert(invoices)
    .values([
      {
        invoiceNumber: 'INV-001',
        carrierId,
        payerName: 'Swift Logistics Inc.',
        payerDaysToPay: 30,
        faceValueMinorUnits: 1250000,
        advanceRateBps: 9000,
        feeRateBps: 200,
        advanceMinorUnits: Math.floor(1250000 * 9000 / 10000),
        feeMinorUnits: Math.floor(1250000 * 200 / 10000),
        reserveMinorUnits: 1250000 - Math.floor(1250000 * 9000 / 10000),
        currency: 'USD',
        status: 'pending',
      },
      {
        invoiceNumber: 'INV-002',
        carrierId,
        underwriterId,
        payerName: 'Northern Freight Co.',
        payerDaysToPay: 45,
        faceValueMinorUnits: 875000,
        advanceRateBps: 8500,
        feeRateBps: 300,
        advanceMinorUnits: Math.floor(875000 * 8500 / 10000),
        feeMinorUnits: Math.floor(875000 * 300 / 10000),
        reserveMinorUnits: 875000 - Math.floor(875000 * 8500 / 10000),
        currency: 'USD',
        status: 'approved',
        approvedAt: now,
      },
      {
        invoiceNumber: 'INV-003',
        carrierId,
        underwriterId,
        payerName: 'Prime Haul Partners',
        payerDaysToPay: 21,
        faceValueMinorUnits: 2300000,
        advanceRateBps: 9200,
        feeRateBps: 150,
        advanceMinorUnits: Math.floor(2300000 * 9200 / 10000),
        feeMinorUnits: Math.floor(2300000 * 150 / 10000),
        reserveMinorUnits: 2300000 - Math.floor(2300000 * 9200 / 10000),
        currency: 'USD',
        status: 'disbursed',
        approvedAt: now,
        disbursedAt: now,
      },
      {
        invoiceNumber: 'INV-004',
        carrierId,
        underwriterId,
        payerName: 'Westbound Cargo LLC',
        payerDaysToPay: 60,
        faceValueMinorUnits: 560000,
        advanceRateBps: 8000,
        feeRateBps: 400,
        advanceMinorUnits: Math.floor(560000 * 8000 / 10000),
        feeMinorUnits: Math.floor(560000 * 400 / 10000),
        reserveMinorUnits: 560000 - Math.floor(560000 * 8000 / 10000),
        currency: 'USD',
        status: 'rejected',
      },
      {
        invoiceNumber: 'INV-005',
        carrierId,
        underwriterId,
        payerName: 'Eagle Transport',
        payerDaysToPay: 30,
        faceValueMinorUnits: 1875000,
        advanceRateBps: 9000,
        feeRateBps: 200,
        advanceMinorUnits: Math.floor(1875000 * 9000 / 10000),
        feeMinorUnits: Math.floor(1875000 * 200 / 10000),
        reserveMinorUnits: 1875000 - Math.floor(1875000 * 9000 / 10000),
        currency: 'USD',
        status: 'collected',
        approvedAt: now,
        disbursedAt: now,
        collectedAt: now,
      },
    ])
    .onConflictDoNothing();

  console.log('Invoices seeded (5 invoices)');
  console.log('\nSeed complete! Credentials:');
  console.log('  admin@factoringdemo.com       / Admin@12345');
  console.log('  underwriter@factoringdemo.com / Under@12345');
  console.log('  carrier@factoringdemo.com     / Carrier@12345');

  await client.end();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
