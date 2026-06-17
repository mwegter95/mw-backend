'use client';
import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

export default function UnderwriterRoot() {
  const router = useRouter();
  useEffect(() => {
    router.replace('/underwriter/queue');
  }, [router]);
  return null;
}
