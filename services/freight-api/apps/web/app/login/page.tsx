'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { authApi } from '../../lib/api';
import { setToken, setUser } from '../../lib/auth';
import type { AuthUser } from '../../lib/auth';

const BASE = '';

const DEMO_CREDS = [
  { label: 'Admin', email: 'admin@factoringdemo.com', password: 'Admin@12345', color: '#F59E0B' },
  { label: 'Underwriter', email: 'underwriter@factoringdemo.com', password: 'Under@12345', color: '#6366F1' },
  { label: 'Carrier', email: 'carrier@factoringdemo.com', password: 'Carrier@12345', color: '#10B981' },
];

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError('');

    try {
      const res = await authApi.login(email, password);
      setToken(res.access_token);
      setUser({
        sub: res.user.id,
        email: res.user.email,
        role: res.user.role as AuthUser['role'],
        name: res.user.name,
      });
      router.replace(`${BASE}/dashboard/`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  const quickLogin = (cred: typeof DEMO_CREDS[0]) => {
    setEmail(cred.email);
    setPassword(cred.password);
  };

  return (
    <div style={{
      minHeight: '100vh',
      background: 'var(--bg-base)',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: '24px',
    }}>
      <div style={{ width: '100%', maxWidth: '420px' }}>
        {/* Logo */}
        <div style={{ textAlign: 'center', marginBottom: '36px' }}>
          <div style={{ fontSize: '3rem', marginBottom: '12px' }}>🚛</div>
          <h1 style={{ fontSize: '1.75rem', fontWeight: 800, color: 'var(--text-primary)', marginBottom: '6px' }}>
            FreightFactor
          </h1>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.875rem' }}>
            Freight Factoring & Banking Operations Console
          </p>
        </div>

        {/* Card */}
        <div className="card" style={{ padding: '32px' }}>
          <h2 style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: '6px' }}>Sign in</h2>
          <p style={{ color: 'var(--text-muted)', fontSize: '0.85rem', marginBottom: '24px' }}>
            Use a demo account or enter credentials
          </p>

          {/* Quick login buttons */}
          <div style={{ display: 'flex', gap: '8px', marginBottom: '24px' }}>
            {DEMO_CREDS.map(cred => (
              <button
                key={cred.label}
                onClick={() => quickLogin(cred)}
                style={{
                  flex: 1,
                  padding: '8px 4px',
                  borderRadius: '8px',
                  border: `1px solid ${cred.color}44`,
                  background: `${cred.color}11`,
                  color: cred.color,
                  fontSize: '0.75rem',
                  fontWeight: 700,
                  cursor: 'pointer',
                  transition: 'background 0.15s',
                  textAlign: 'center',
                }}
                onMouseEnter={e => { (e.currentTarget as HTMLElement).style.background = `${cred.color}22`; }}
                onMouseLeave={e => { (e.currentTarget as HTMLElement).style.background = `${cred.color}11`; }}
              >
                {cred.label}
              </button>
            ))}
          </div>
          <div style={{ textAlign: 'center', color: 'var(--text-muted)', fontSize: '0.75rem', marginBottom: '20px' }}>
            Quick-fill above, then sign in — or type manually
          </div>

          {error && <div className="error-box">{error}</div>}

          <form onSubmit={handleSubmit}>
            <div className="form-group">
              <label className="form-label" htmlFor="email">Email</label>
              <input
                id="email"
                type="email"
                className="input"
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="you@example.com"
                required
                autoComplete="email"
              />
            </div>
            <div className="form-group">
              <label className="form-label" htmlFor="password">Password</label>
              <input
                id="password"
                type="password"
                className="input"
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder="••••••••"
                required
                autoComplete="current-password"
              />
            </div>
            <button
              type="submit"
              className="btn-primary"
              style={{ width: '100%', marginTop: '8px' }}
              disabled={loading}
            >
              {loading ? 'Signing in...' : 'Sign in'}
            </button>
          </form>
        </div>

        {/* Credentials hint */}
        <div style={{
          marginTop: '20px',
          padding: '16px',
          background: 'var(--bg-surface)',
          borderRadius: '10px',
          border: '1px solid var(--bg-border)',
          fontSize: '0.78rem',
          color: 'var(--text-muted)',
        }}>
          <div style={{ fontWeight: 700, color: 'var(--text-secondary)', marginBottom: '8px' }}>Demo credentials</div>
          {DEMO_CREDS.map(c => (
            <div key={c.email} style={{ marginBottom: '4px', fontFamily: 'var(--font-mono)' }}>
              <span style={{ color: c.color }}>{c.label}:</span>{' '}
              {c.email} / {c.password}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
