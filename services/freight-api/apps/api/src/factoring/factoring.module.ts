import { Module } from '@nestjs/common';
import { FactoringController } from './factoring.controller';
import { FactoringService } from './factoring.service';

@Module({
  controllers: [FactoringController],
  providers: [FactoringService],
})
export class FactoringModule {}
