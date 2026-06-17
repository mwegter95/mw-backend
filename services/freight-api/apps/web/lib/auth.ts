'use client';

export interface AuthUser {
  sub: number;
  email: string;
  role: 'admin' | 'underwriter' | 'carrier' | 'driver';
  name: string;
}

export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem('factoring_token');
}

export function setToken(token: string): void {
  localStorage.setItem('factoring_token', token);
}

export function clearToken(): void {
  localStorage.removeItem('factoring_token');
  localStorage.removeItem('factoring_user');
}

export function getUser(): AuthUser | null {
  if (typeof window === 'undefined') return null;
  const raw = localStorage.getItem('factoring_user');
  if (!raw) return null;
  try { return JSON.parse(raw) as AuthUser; }
  catch { return null; }
}

export function setUser(user: AuthUser): void {
  localStorage.setItem('factoring_user', JSON.stringify(user));
}

export function hasRole(user: AuthUser | null, ...roles: string[]): boolean {
  if (!user) return false;
  return roles.includes(user.role);
}
