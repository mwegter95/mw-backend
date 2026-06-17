'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { getUser, clearToken } from '../lib/auth';
import { isMockMode } from '../lib/mockData';
import { useState, useEffect } from 'react';
import type { AuthUser } from '../lib/auth';

const BASE = '';

function NavLink({ href, children, icon }: { href: string; children: React.ReactNode; icon: string }) {
  const pathname = usePathname();
  const active = pathname === href || pathname.startsWith(href + '/');

  return (
    <Link
      href={href}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '10px',
        padding: '9px 14px',
        borderRadius: '8px',
        color: active ? 'var(--accent)' : 'var(--text-secondary)',
        background: active ? 'var(--accent-muted)' : 'transparent',
        border: active ? '1px solid rgba(16,185,129,0.2)' : '1px solid transparent',
        textDecoration: 'none',
        fontSize: '0.875rem',
        fontWeight: active ? 600 : 400,
        transition: 'all 0.15s',
      }}
      onMouseEnter={e => {
        if (!active) {
          (e.currentTarget as HTMLElement).style.background = 'var(--bg-surface-2)';
          (e.currentTarget as HTMLElement).style.color = 'var(--text-primary)';
        }
      }}
      onMouseLeave={e => {
        if (!active) {
          (e.currentTarget as HTMLElement).style.background = 'transparent';
          (e.currentTarget as HTMLElement).style.color = 'var(--text-secondary)';
        }
      }}
    >
      <span style={{ fontSize: '1rem', width: '18px', textAlign: 'center' }}>{icon}</span>
      {children}
    </Link>
  );
}

export function Sidebar() {
  const router = useRouter();
  const [user, setUser] = useState<AuthUser | null>(null);
  const [mockMode, setMockMode] = useState(false);

  useEffect(() => {
    setUser(getUser());
    setMockMode(isMockMode());
  }, []);

  const handleLogout = () => {
    clearToken();
    router.push(`${BASE}/login/`);
  };

  const roleColors: Record<string, string> = {
    admin: '#F59E0B',
    underwriter: '#6366F1',
    carrier: '#10B981',
    driver: '#3B82F6',
  };

  const roleColor = user ? (roleColors[user.role] ?? '#9CA3AF') : '#9CA3AF';

  return (
    <aside style={{
      width: '240px',
      minHeight: '100vh',
      background: 'var(--bg-surface)',
      borderRight: '1px solid var(--bg-border)',
      display: 'flex',
      flexDirection: 'column',
      padding: '0',
      flexShrink: 0,
    }}>
      {/* Logo */}
      <div style={{
        padding: '22px 20px',
        borderBottom: '1px solid var(--bg-border)',
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
      }}>
        <span style={{ fontSize: '1.6rem' }}>🚛</span>
        <div>
          <div style={{ color: 'var(--text-primary)', fontWeight: 700, fontSize: '0.95rem', lineHeight: 1.2 }}>
            FreightFactor
          </div>
          <div style={{ color: 'var(--accent)', fontSize: '0.7rem', fontWeight: 500, letterSpacing: '0.06em', textTransform: 'uppercase' }}>
            Operations Console
          </div>
        </div>
      </div>

      {/* Demo mode badge */}
      {mockMode && (
        <div style={{
          margin: '8px 12px 0',
          padding: '6px 10px',
          background: '#78350F22',
          border: '1px solid #78350F55',
          borderRadius: '7px',
          fontSize: '0.68rem',
          color: '#F59E0B',
          fontWeight: 600,
          textAlign: 'center',
          letterSpacing: '0.04em',
        }}>
          DEMO MODE — mock data
        </div>
      )}

      {/* Navigation */}
      <nav style={{ padding: '16px 12px', flex: 1, display: 'flex', flexDirection: 'column', gap: '2px' }}>
        <SectionLabel>Overview</SectionLabel>
        <NavLink href={`${BASE}/dashboard`} icon="⬛">Dashboard</NavLink>

        {user && (user.role === 'carrier') && (
          <>
            <SectionLabel>Factoring</SectionLabel>
            <NavLink href={`${BASE}/invoices/new`} icon="➕">Submit Invoice</NavLink>
            <NavLink href={`${BASE}/invoices`} icon="📄">My Invoices</NavLink>
          </>
        )}

        {user && (user.role === 'underwriter' || user.role === 'admin') && (
          <>
            <SectionLabel>Underwriting</SectionLabel>
            <NavLink href={`${BASE}/underwriter/queue`} icon="📋">Approval Queue</NavLink>
            <NavLink href={`${BASE}/invoices`} icon="📄">All Invoices</NavLink>
          </>
        )}

        <SectionLabel>Banking</SectionLabel>
        <NavLink href={`${BASE}/accounts`} icon="🏦">Accounts</NavLink>
        <NavLink href={`${BASE}/ledger`} icon="📒">Ledger</NavLink>
      </nav>

      {/* User badge */}
      {user && (
        <div style={{
          padding: '16px',
          borderTop: '1px solid var(--bg-border)',
        }}>
          <div style={{
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            padding: '10px 12px',
            background: 'var(--bg-surface-2)',
            borderRadius: '10px',
          }}>
            <div style={{
              width: '34px',
              height: '34px',
              borderRadius: '50%',
              background: `${roleColor}22`,
              border: `2px solid ${roleColor}55`,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              fontSize: '0.9rem',
              fontWeight: 700,
              color: roleColor,
              flexShrink: 0,
            }}>
              {user.name[0]}
            </div>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{
                color: 'var(--text-primary)',
                fontSize: '0.8rem',
                fontWeight: 600,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}>
                {user.name}
              </div>
              <div style={{
                color: roleColor,
                fontSize: '0.7rem',
                fontWeight: 500,
                textTransform: 'capitalize',
              }}>
                {user.role}
              </div>
            </div>
            <button
              onClick={handleLogout}
              title="Logout"
              style={{
                background: 'none',
                border: 'none',
                cursor: 'pointer',
                color: 'var(--text-muted)',
                fontSize: '1rem',
                padding: '2px',
                borderRadius: '4px',
                lineHeight: 1,
              }}
            >
              ⇥
            </button>
          </div>
        </div>
      )}
    </aside>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div style={{
      color: 'var(--text-muted)',
      fontSize: '0.65rem',
      fontWeight: 700,
      textTransform: 'uppercase',
      letterSpacing: '0.1em',
      padding: '12px 14px 4px',
    }}>
      {children}
    </div>
  );
}
