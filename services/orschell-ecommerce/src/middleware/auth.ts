import { Request, Response, NextFunction } from 'express';
import jwt from 'jsonwebtoken';
import { db } from '../db/database';
import type { AuthPayload } from '../types';

const JWT_SECRET = process.env.ORSCHELL_JWT_SECRET || 'orschell-dev-secret-change-in-prod';

export function makeToken(payload: AuthPayload): string {
  return jwt.sign(payload, JWT_SECRET, { expiresIn: '7d' });
}

export function requireAuth(req: Request, res: Response, next: NextFunction): void {
  const auth = req.headers.authorization;
  if (!auth?.startsWith('Bearer ')) {
    res.status(401).json({ error: 'Authentication required' });
    return;
  }
  try {
    const payload = jwt.verify(auth.slice(7), JWT_SECRET) as AuthPayload;
    const user = db.prepare('SELECT id FROM users WHERE id = ?').get(payload.userId) as any;
    if (!user) {
      res.status(401).json({ error: 'Account not found' });
      return;
    }
    req.user = payload;
    next();
  } catch {
    res.status(401).json({ error: 'Invalid or expired token' });
  }
}

export function requireAdmin(req: Request, res: Response, next: NextFunction): void {
  requireAuth(req, res, () => {
    if (req.user?.role !== 'admin') {
      res.status(403).json({ error: 'Admin access required' });
      return;
    }
    next();
  });
}
