import { Controller, Get, Param, ParseIntPipe, UseGuards } from '@nestjs/common';
import { LedgerService } from './ledger.service';
import { JwtAuthGuard } from '../auth/jwt.strategy';

@UseGuards(JwtAuthGuard)
@Controller()
export class LedgerController {
  constructor(private readonly service: LedgerService) {}

  @Get('accounts')
  listAccounts() {
    return this.service.listAccounts();
  }

  @Get('accounts/:id/entries')
  accountEntries(@Param('id', ParseIntPipe) id: number) {
    return this.service.getAccountWithEntries(id);
  }

  @Get('ledger')
  recent() {
    return this.service.recentEntries();
  }
}
