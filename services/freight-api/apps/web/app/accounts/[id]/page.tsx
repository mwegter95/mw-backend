import AccountDetailClient from './client';

export function generateStaticParams() {
  return [1, 2, 3, 4, 5, 6, 7, 8].map((id) => ({ id: String(id) }));
}

export default async function AccountDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <AccountDetailClient id={parseInt(id, 10)} />;
}
