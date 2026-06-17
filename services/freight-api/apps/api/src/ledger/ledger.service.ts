import { Injectable, Inject } from '@nestjs/common';
import { desc, eq } from 'drizzle-orm';
import { DB_TOKEN } from '../db/db.module';
import { accounts, ledgerEntries } from '../../../../packages/db/src/schema';
import type { NodePgDatabase } from 'drizzle-orm/node-postgres';
import type * as schema from '../../../../packages/db/src/schema';

type DB = NodePgDatabase<typeof schema>;

@Injectable()
export class LedgerService {
  constructor(@Inject(DB_TOKEN) private readonly db: DB) {}

  async listAccounts() {
    return this.db.select().from(accounts).orderBy(accounts.code);
  }

  async getAccountWithEntries(id: number) {
    const [acct] = await this.db
      .select()
      .from(accounts)
      .where(eq(accounts.id, id))
      .limit(1);

    const entries = await this.db
      .select()
      .from(ledgerEntries)
      .where(eq(ledgerEntries.accountId, id))
      .orderBy(desc(ledgerEntries.postedAt))
      .limit(100);

    return { account: acct, entries };
  }

  async recentEntries() {
    return this.db
      .select()
      .from(ledgerEntries)
      .orderBy(desc(ledgerEntries.postedAt))
      .limit(50);
  }
}
