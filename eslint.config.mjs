import js from '@eslint/js';
import globals from 'globals';

export default [{
  files: ['comic_archive/static/*.js'],
  ...js.configs.recommended,
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: 'script',
    globals: globals.browser,
  },
  rules: {
    ...js.configs.recommended.rules,
    'no-unused-vars': ['error', {argsIgnorePattern: '^_', caughtErrors: 'none'}],
    'eqeqeq': ['error', 'always'],
  },
}];
