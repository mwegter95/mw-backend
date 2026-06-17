import { z } from 'zod';

export const CreateInvoiceSchema = z.object({
  invoiceNumber: z.string().min(1),
  payerName: z.string().min(1),
  payerDaysToPay: z.number().int().positive().max(365).default(30),
  faceValueMinorUnits: z.number().int().positive(),
  advanceRateBps: z.number().int().min(5000).max(9500),
  feeRateBps: z.number().int().min(50).max(1000),
  currency: z.enum(['USD', 'BRL']).default('USD'),
  notes: z.string().optional(),
});

export type CreateInvoiceDto = z.infer<typeof CreateInvoiceSchema>;
