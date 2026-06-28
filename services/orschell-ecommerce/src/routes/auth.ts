import { Router } from 'express';
import * as bcrypt from 'bcryptjs';
import { db } from '../db/database';
import { makeToken, requireAuth } from '../middleware/auth';
import type { RunResult } from '../types';

const router = Router();

router.post('/register', (req, res) => {
  const { email, password, name } = req.body || {};
  if (!email || !password) return res.status(400).json({ error: 'email and password required' });
  if (password.length < 6) return res.status(400).json({ error: 'Password must be at least 6 characters' });
  const existing = db.prepare('SELECT id FROM users WHERE email = ?').get(email.toLowerCase().trim()) as any;
  if (existing) return res.status(409).json({ error: 'Email already registered' });
  const hash = bcrypt.hashSync(password, 10);
  const result = db.prepare('INSERT INTO users (email,password_hash,name,role) VALUES (?,?,?,?)').run(
    email.toLowerCase().trim(), hash, (name || email.split('@')[0]).trim(), 'customer'
  ) as unknown as RunResult;
  const user = db.prepare('SELECT id,email,name,role FROM users WHERE id = ?').get(result.lastInsertRowid) as any;
  return res.status(201).json({ token: makeToken({ userId: user.id, email: user.email, name: user.name, role: user.role }), user });
});

router.post('/login', (req, res) => {
  const { email, password } = req.body || {};
  if (!email || !password) return res.status(400).json({ error: 'email and password required' });
  const user = db.prepare('SELECT * FROM users WHERE email = ?').get(email.toLowerCase().trim()) as any;
  if (!user || !bcrypt.compareSync(password, user.password_hash)) {
    return res.status(401).json({ error: 'Invalid email or password' });
  }
  const payload = { userId: user.id, email: user.email, name: user.name, role: user.role };
  return res.json({ token: makeToken(payload), user: { id: user.id, email: user.email, name: user.name, role: user.role } });
});

router.get('/me', requireAuth, (req, res) => {
  const user = db.prepare('SELECT id,email,name,role,created_at FROM users WHERE id = ?').get(req.user!.userId) as any;
  if (!user) return res.status(404).json({ error: 'User not found' });
  return res.json({ user });
});

export default router;
