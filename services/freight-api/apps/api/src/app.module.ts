import { Module } from '@nestjs/common';
import { AuthModule } from './auth/auth.module';
import { FactoringModule } from './factoring/factoring.module';
import { LedgerModule } from './ledger/ledger.module';
import { DbModule } from './db/db.module';

@Module({
  imports: [DbModule, AuthModule, FactoringModule, LedgerModule],
})
export class AppModule {}
