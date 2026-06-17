import {
  Injectable,
  NotFoundException,
  ForbiddenException,
  BadRequestException,
  Inject,
} from '@nestjs/common';
import { eq, desc, and } from 'drizzle-orm';
import { DB_TOKEN } from '../db/db.module';
import {
  invoices,
  ledgerEntries,
  accounts,
} from '../../../../packages/db/src/schema';
import type { CreateInvoiceDto } from './dto/create-invoice.dto';
import { CreateInvoiceSchema } from './dto/create-invoice.dto';
import type { JwtPayload } from '../auth/jwt.strategy';
import type { NodePgDatabase } from 'drizzle-orm/node-postgres';
import type * as schema from '../../../../packages/db/src/schema';

type DB = NodePgDatabase<typeof schema>;

// Integer-only money math — no floats on money paths
function calcAdvance(faceValueMinorUnits: number, advanceRateBps: number): number {
  return Math.floor((faceValueMinorUnits * advanceRateBps) / 10000);
}
function calcFee(faceValueMinorUnits: number, feeRateBps: number): number {
  return Math.floor((faceValueMinorUnits * feeRateBps) / 10000);
}
function calcReserve(faceValueMinorUnits: number, advanceMinorUnits: number): number {
  return faceValueMinorUnits - advanceMinorUnits;
}

@Injectable()
export class FactoringService {
  constructor(@Inject(DB_TOKEN) private readonly db: DB) {}

  async createInvoice(body: unknown, user: JwtPayload) {
    const dto: CreateInvoiceDto = CreateInvoiceSchema.parse(body);

    const advanceMinorUnits = calcAdvance(dto.faceValueMinorUnits, dto.advanceRateBps);
    const feeMinorUnits = calcFee(dto.faceValueMinorUnits, dto.feeRateBps);
    const reserveMinorUnits = calcReserve(dto.faceValueMinorUnits, advanceMinorUnits);

    const [invoice] = await this.db
      .insert(invoices)
      .values({
        invoiceNumber: dto.invoiceNumber,
        carrierId: user.sub,
        payerName: dto.payerName,
        payerDaysToPay: dto.payerDaysToPay,
        faceValueMinorUnits: dto.faceValueMinorUnits,
        advanceRateBps: dto.advanceRateBps,
        feeRateBps: dto.feeRateBps,
        advanceMinorUnits,
        feeMinorUnits,
        reserveMinorUnits,
        currency: dto.currency,
        notes: dto.notes,
        status: 'pending',
      })
      .returning();

    return invoice;
  }

  async listInvoices(user: JwtPayload) {
    if (user.role === 'carrier') {
      return this.db
        .select()
        .from(invoices)
        .where(eq(invoices.carrierId, user.sub))
        .orderBy(desc(invoices.submittedAt));
    }
    // underwriter, admin see all
    return this.db
      .select()
      .from(invoices)
      .orderBy(desc(invoices.submittedAt));
  }

  async getInvoice(id: number, user: JwtPayload) {
    const [inv] = await this.db
      .select()
      .from(invoices)
      .where(eq(invoices.id, id))
      .limit(1);

    if (!inv) throw new NotFoundException(`Invoice ${id} not found`);

    if (user.role === 'carrier' && inv.carrierId !== user.sub) {
      throw new ForbiddenException('Not your invoice');
    }

    return inv;
  }

  async approveInvoice(id: number, user: JwtPayload, notes?: string) {
    if (!['underwriter', 'admin'].includes(user.role)) {
      throw new ForbiddenException('Insufficient role');
    }

    const [inv] = await this.db
      .select()
      .from(invoices)
      .where(eq(invoices.id, id))
      .limit(1);

    if (!inv) throw new NotFoundException(`Invoice ${id} not found`);
    if (inv.status !== 'pending') {
      throw new BadRequestException(`Invoice is in '${inv.status}' status, cannot approve`);
    }

    const [updated] = await this.db
      .update(invoices)
      .set({
        status: 'approved',
        underwriterId: user.sub,
        approvedAt: new Date(),
        notes: notes ?? inv.notes,
      })
      .where(eq(invoices.id, id))
      .returning();

    return updated;
  }

  async rejectInvoice(id: number, user: JwtPayload, notes?: string) {
    if (!['underwriter', 'admin'].includes(user.role)) {
      throw new ForbiddenException('Insufficient role');
    }

    const [inv] = await this.db
      .select()
      .from(invoices)
      .where(eq(invoices.id, id))
      .limit(1);

    if (!inv) throw new NotFoundException(`Invoice ${id} not found`);
    if (inv.status !== 'pending') {
      throw new BadRequestException(`Invoice is in '${inv.status}' status, cannot reject`);
    }

    const [updated] = await this.db
      .update(invoices)
      .set({
        status: 'rejected',
        underwriterId: user.sub,
        notes: notes ?? inv.notes,
      })
      .where(eq(invoices.id, id))
      .returning();

    return updated;
  }

  async disburseInvoice(id: number, user: JwtPayload) {
    if (!['underwriter', 'admin'].includes(user.role)) {
      throw new ForbiddenException('Insufficient role');
    }

    const [inv] = await this.db
      .select()
      .from(invoices)
      .where(eq(invoices.id, id))
      .limit(1);

    if (!inv) throw new NotFoundException(`Invoice ${id} not found`);
    if (inv.status !== 'approved') {
      throw new BadRequestException(`Invoice must be 'approved' to disburse`);
    }

    const advanceAmt = inv.advanceMinorUnits!;

    // Look up accounts
    const [recAcct] = await this.db
      .select()
      .from(accounts)
      .where(eq(accounts.code, 'FACTORING_REC'))
      .limit(1);
    const [cashAcct] = await this.db
      .select()
      .from(accounts)
      .where(eq(accounts.code, 'CASH_BANK'))
      .limit(1);

    if (!recAcct || !cashAcct) throw new BadRequestException('Ledger accounts not seeded');

    // Post double-entry: Debit FACTORING_REC (asset increases), Credit CASH_BANK (asset decreases)
    await this.db.insert(ledgerEntries).values([
      {
        invoiceId: id,
        accountId: recAcct.id,
        entryType: 'debit',
        amountMinorUnits: advanceAmt,
        currency: inv.currency,
        description: `Advance disbursed for ${inv.invoiceNumber}`,
      },
      {
        invoiceId: id,
        accountId: cashAcct.id,
        entryType: 'credit',
        amountMinorUnits: advanceAmt,
        currency: inv.currency,
        description: `Cash out — advance for ${inv.invoiceNumber}`,
      },
    ]);

    // Update account balances (integer arithmetic only)
    await this.db
      .update(accounts)
      .set({
        balanceMinorUnits: recAcct.balanceMinorUnits + advanceAmt,
        updatedAt: new Date(),
      })
      .where(eq(accounts.id, recAcct.id));

    await this.db
      .update(accounts)
      .set({
        balanceMinorUnits: cashAcct.balanceMinorUnits - advanceAmt,
        updatedAt: new Date(),
      })
      .where(eq(accounts.id, cashAcct.id));

    const [updated] = await this.db
      .update(invoices)
      .set({ status: 'disbursed', disbursedAt: new Date() })
      .where(eq(invoices.id, id))
      .returning();

    return updated;
  }

  async collectInvoice(id: number, user: JwtPayload) {
    if (user.role !== 'admin') {
      throw new ForbiddenException('Only admins can mark collected');
    }

    const [inv] = await this.db
      .select()
      .from(invoices)
      .where(eq(invoices.id, id))
      .limit(1);

    if (!inv) throw new NotFoundException(`Invoice ${id} not found`);
    if (inv.status !== 'disbursed') {
      throw new BadRequestException(`Invoice must be 'disbursed' to collect`);
    }

    const faceAmt = inv.faceValueMinorUnits;
    const advanceAmt = inv.advanceMinorUnits!;
    const feeAmt = inv.feeMinorUnits!;
    const reserveAmt = inv.reserveMinorUnits!;

    const [recAcct] = await this.db.select().from(accounts).where(eq(accounts.code, 'FACTORING_REC')).limit(1);
    const [cashAcct] = await this.db.select().from(accounts).where(eq(accounts.code, 'CASH_BANK')).limit(1);
    const [feeAcct] = await this.db.select().from(accounts).where(eq(accounts.code, 'FEE_REVENUE')).limit(1);
    const [reserveAcct] = await this.db.select().from(accounts).where(eq(accounts.code, 'CARRIER_RESERVE')).limit(1);

    if (!recAcct || !cashAcct || !feeAcct || !reserveAcct) {
      throw new BadRequestException('Ledger accounts not seeded');
    }

    // Collection: payer pays full face value
    // Debit CASH_BANK (cash in), Credit FACTORING_REC (close receivable), Credit FEE_REVENUE, release reserve to carrier
    await this.db.insert(ledgerEntries).values([
      {
        invoiceId: id,
        accountId: cashAcct.id,
        entryType: 'debit',
        amountMinorUnits: faceAmt,
        currency: inv.currency,
        description: `Collection received for ${inv.invoiceNumber}`,
      },
      {
        invoiceId: id,
        accountId: recAcct.id,
        entryType: 'credit',
        amountMinorUnits: advanceAmt,
        currency: inv.currency,
        description: `Close receivable — ${inv.invoiceNumber}`,
      },
      {
        invoiceId: id,
        accountId: feeAcct.id,
        entryType: 'credit',
        amountMinorUnits: feeAmt,
        currency: inv.currency,
        description: `Factoring fee earned — ${inv.invoiceNumber}`,
      },
      {
        invoiceId: id,
        accountId: reserveAcct.id,
        entryType: 'credit',
        amountMinorUnits: reserveAmt - feeAmt,
        currency: inv.currency,
        description: `Reserve released to carrier — ${inv.invoiceNumber}`,
      },
    ]);

    // Update balances
    await this.db.update(accounts).set({
      balanceMinorUnits: cashAcct.balanceMinorUnits + faceAmt,
      updatedAt: new Date(),
    }).where(eq(accounts.id, cashAcct.id));

    await this.db.update(accounts).set({
      balanceMinorUnits: recAcct.balanceMinorUnits - advanceAmt,
      updatedAt: new Date(),
    }).where(eq(accounts.id, recAcct.id));

    await this.db.update(accounts).set({
      balanceMinorUnits: feeAcct.balanceMinorUnits + feeAmt,
      updatedAt: new Date(),
    }).where(eq(accounts.id, feeAcct.id));

    const [updated] = await this.db
      .update(invoices)
      .set({ status: 'collected', collectedAt: new Date() })
      .where(eq(invoices.id, id))
      .returning();

    return updated;
  }
}
