import { Module, Global } from '@nestjs/common';
import { drizzle } from 'drizzle-orm/postgres-js';
import postgres from 'postgres';
import * as schema from '../../../../packages/db/src/schema';

export const DB_TOKEN = 'DRIZZLE_DB';

@Global()
@Module({
  providers: [
    {
      provide: DB_TOKEN,
      useFactory: () => {
        const client = postgres(process.env.DATABASE_URL!, { prepare: false });
        return drizzle(client, { schema });
      },
    },
  ],
  exports: [DB_TOKEN],
})
export class DbModule {}
