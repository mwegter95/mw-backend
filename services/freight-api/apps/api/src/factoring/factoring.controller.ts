import {
  Controller,
  Get,
  Post,
  Patch,
  Body,
  Param,
  ParseIntPipe,
  UseGuards,
  Request,
} from '@nestjs/common';
import { FactoringService } from './factoring.service';
import { JwtAuthGuard } from '../auth/jwt.strategy';
import type { JwtPayload } from '../auth/jwt.strategy';

interface AuthRequest {
  user: JwtPayload;
}

@UseGuards(JwtAuthGuard)
@Controller('invoices')
export class FactoringController {
  constructor(private readonly service: FactoringService) {}

  @Post()
  create(@Body() body: unknown, @Request() req: AuthRequest) {
    return this.service.createInvoice(body, req.user);
  }

  @Get()
  list(@Request() req: AuthRequest) {
    return this.service.listInvoices(req.user);
  }

  @Get(':id')
  getOne(@Param('id', ParseIntPipe) id: number, @Request() req: AuthRequest) {
    return this.service.getInvoice(id, req.user);
  }

  @Patch(':id/approve')
  approve(
    @Param('id', ParseIntPipe) id: number,
    @Body() body: { notes?: string },
    @Request() req: AuthRequest,
  ) {
    return this.service.approveInvoice(id, req.user, body.notes);
  }

  @Patch(':id/reject')
  reject(
    @Param('id', ParseIntPipe) id: number,
    @Body() body: { notes?: string },
    @Request() req: AuthRequest,
  ) {
    return this.service.rejectInvoice(id, req.user, body.notes);
  }

  @Patch(':id/disburse')
  disburse(
    @Param('id', ParseIntPipe) id: number,
    @Request() req: AuthRequest,
  ) {
    return this.service.disburseInvoice(id, req.user);
  }

  @Patch(':id/collect')
  collect(
    @Param('id', ParseIntPipe) id: number,
    @Request() req: AuthRequest,
  ) {
    return this.service.collectInvoice(id, req.user);
  }
}
