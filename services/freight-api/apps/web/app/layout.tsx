import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'FreightFactor — Operations Console',
  description: 'Freight factoring and banking operations platform. NestJS + Drizzle + Postgres monorepo with FIDC ledger, RBAC, and integer-cents money discipline.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
