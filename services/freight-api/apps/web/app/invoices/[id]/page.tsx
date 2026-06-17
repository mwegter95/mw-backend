import InvoiceDetailClient from './client';

export function generateStaticParams() {
  return [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((id) => ({ id: String(id) }));
}

export default async function InvoiceDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <InvoiceDetailClient id={parseInt(id, 10)} />;
}
