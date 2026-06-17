import { z } from 'zod';

export const ApproveInvoiceSchema = z.object({
  notes: z.string().optional(),
});

export type ApproveInvoiceDto = z.infer<typeof ApproveInvoiceSchema>;
