// The official entrypoint runs this only when /data/db is empty.
const appDb = db.getSiblingDB('messages');
const password = require('fs').readFileSync('/run/secrets/mongo_password', 'utf8').replace(/[\r\n]+$/, '');
if (!password.trim() || /[\r\n\x00]/.test(password)) throw new Error('Invalid application secret');
if (!appDb.getUser('messages_app')) appDb.createUser({
  user: 'messages_app',
  pwd: password,
  roles: [{role: 'readWrite', db: 'messages'}],
});
