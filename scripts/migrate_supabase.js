#!/usr/bin/env node

/**
 * Author: Murtaza Nuruddin
 * Automates migration of frontend code from Supabase patterns to Django REST API patterns.
 *
 * The script scans `src/**/*.ts` and `src/**/*.tsx`, applies a set of regex-based
 * replacements for common Supabase auth, RPC, function, and table query patterns,
 * adds required imports such as `useAuth` and `apiClient`, and writes backups for
 * modified files unless run in dry-run mode.
 *
 * Usage:
 *   node migrate-supabase-to-django.js [--dry-run] [--verbose]
 *
 * Options:
 * - `--dry-run`   Show what would change without modifying files
 * - `--verbose`   Print detailed pattern-level migration output
 *
 * Notes:
 * - Only files containing `supabase.` are processed
 * - Backup copies are saved with a `.backup` extension before overwriting
 * - Migration is regex-based, so all changes should be reviewed manually
 * - After running, check diffs, TypeScript errors, build output, and runtime behavior
 */

import fs from 'fs';
import path from 'path';
import { glob } from 'glob';

// Configuration
const DRY_RUN = process.argv.includes('--dry-run');
const VERBOSE = process.argv.includes('--verbose');

// Statistics
const stats = {
  filesScanned: 0,
  filesModified: 0,
  filesSkipped: 0,
  patternsApplied: 0,
  errors: []
};

// Migration patterns
const patterns = [
  // Pattern 1: Auth getSession() -> useAuth hook
  {
    name: 'auth-get-session',
    regex: /const\s+{\s*data:\s*{\s*session\s*}\s*}\s*=\s*await\s+supabase\.auth\.getSession\(\);?\s*\n?\s*(?:if\s*\(!session\?\.user\)\s*throw\s*new\s*Error\([^)]+\);?\s*\n?\s*)?(?:const\s+user\s*=\s*session\.user;?)?/g,
    replacement: '// Migrated: Use useAuth hook\n  const { user } = useAuth();\n  if (!user) throw new Error("Not authenticated");',
    needsImport: 'useAuth'
  },
  
  // Pattern 2: Auth getUser() -> useAuth hook
  {
    name: 'auth-get-user',
    regex: /const\s+{\s*data:\s*{\s*user\s*}\s*}\s*=\s*await\s+supabase\.auth\.getUser\(\);/g,
    replacement: '// Migrated: Use useAuth hook\n  const { user } = useAuth();',
    needsImport: 'useAuth'
  },
  
  // Pattern 3: Auth signOut() -> logout()
  {
    name: 'auth-sign-out',
    regex: /await\s+supabase\.auth\.signOut\(\);/g,
    replacement: 'const { logout } = useAuth();\n  await logout();',
    needsImport: 'useAuth'
  },
  
  // Pattern 4: Auth refreshSession() -> handled by apiClient
  {
    name: 'auth-refresh-session',
    regex: /await\s+supabase\.auth\.refreshSession\(\);/g,
    replacement: '// Migrated: Token refresh handled automatically by apiClient',
    needsImport: null
  },
  
  // Pattern 5: RPC calls -> apiClient.post
  {
    name: 'rpc-call',
    regex: /const\s+{\s*data(?:,\s*error)?\s*}\s*=\s*await\s+supabase\.rpc\(\s*['"]([^'"]+)['"]\s*,\s*({[^}]+})\s*\);/g,
    replacement: (match, funcName, params) => {
      const kebabName = funcName.replace(/_/g, '-');
      // Convert p_param to param
      const cleanParams = params.replace(/p_(\w+)/g, '$1');
      return `// Migrated: RPC call\n  const response = await apiClient.post('/rpc/${kebabName}/', ${cleanParams});\n  const data = response.data;`;
    },
    needsImport: 'apiClient'
  },
  
  // Pattern 6: Function invoke -> apiClient.post
  {
    name: 'function-invoke',
    regex: /const\s+{\s*data(?:,\s*error(?::\s*\w+)?)?\s*}\s*=\s*await\s+supabase\.functions\.invoke\(\s*['"]([^'"]+)['"]\s*,\s*{\s*body:\s*({[^}]+})\s*}\s*\);/g,
    replacement: (match, funcName, body) => {
      return `// Migrated: Function invoke\n  const response = await apiClient.post('/functions/${funcName}/', ${body});\n  const data = response.data;`;
    },
    needsImport: 'apiClient'
  },
  
  // Pattern 7: Simple function invoke without body
  {
    name: 'function-invoke-simple',
    regex: /const\s+{\s*data(?:,\s*error)?\s*}\s*=\s*await\s+supabase\.functions\.invoke\(\s*['"]([^'"]+)['"]\s*\);/g,
    replacement: (match, funcName) => {
      return `// Migrated: Function invoke\n  const response = await apiClient.post('/functions/${funcName}/');\n  const data = response.data;`;
    },
    needsImport: 'apiClient'
  },
  
  // Pattern 8: Table select -> useCrudRead (simple case)
  {
    name: 'table-select-simple',
    regex: /const\s+{\s*data(?:,\s*error)?\s*}\s*=\s*await\s+supabase\.from\(\s*['"]([^'"]+)['"]\s*\)\.select\(\s*['"][^'"]*['"]\s*\)\.eq\(\s*['"]([^'"]+)['"]\s*,\s*([^)]+)\s*\);/g,
    replacement: (match, tableName, field, value) => {
      const kebabTable = tableName.replace(/_/g, '-');
      return `// Migrated: Table query\n  const response = await apiClient.get('/${kebabTable}/', { params: { ${field}: ${value} } });\n  const data = response.data;`;
    },
    needsImport: 'apiClient'
  },
  
  // Pattern 9: Table insert -> apiClient.post
  {
    name: 'table-insert',
    regex: /const\s+{\s*(?:data(?:,\s*)?)?error\s*}\s*=\s*await\s+supabase\.from\(\s*['"]([^'"]+)['"]\s*\)\.insert\(([^)]+)\);/g,
    replacement: (match, tableName, data) => {
      const kebabTable = tableName.replace(/_/g, '-');
      return `// Migrated: Table insert\n  const response = await apiClient.post('/${kebabTable}/', ${data});\n  const insertedData = response.data;`;
    },
    needsImport: 'apiClient'
  },
  
  // Pattern 10: OAuth signIn -> Comment with TODO
  {
    name: 'oauth-signin',
    regex: /await\s+supabase\.auth\.signInWithOAuth\(\s*{\s*provider:\s*['"](\w+)['"]\s*[^}]*}\s*\);/g,
    replacement: (match, provider) => {
      return `// TODO: Implement OAuth redirect\n  // window.location.href = \`\${import.meta.env.VITE_API_URL}/auth/oauth/${provider}/\`;`;
    },
    needsImport: null
  },
  
  // Pattern 11: signUp -> apiClient.post
  {
    name: 'auth-signup',
    regex: /const\s+{\s*data(?::\s*authData)?(?:,\s*error(?::\s*\w+)?)?\s*}\s*=\s*await\s+supabase\.auth\.signUp\(\s*({[^}]+})\s*\);/g,
    replacement: (match, credentials) => {
      return `// Migrated: Sign up\n  const response = await apiClient.post('/auth/register/', ${credentials});\n  const authData = response.data;`;
    },
    needsImport: 'apiClient'
  }
];

// Add necessary imports to file
function addImports(content, neededImports) {
  const imports = new Set(neededImports.filter(Boolean));
  
  if (imports.size === 0) return content;
  
  let modified = content;
  
  // Check if imports already exist
  if (imports.has('useAuth') && !content.includes("from '@/hooks/useAuth'") && !content.includes('from "@/hooks/useAuth"')) {
    modified = `import { useAuth } from '@/hooks/useAuth';\n${modified}`;
  }
  
  if (imports.has('apiClient') && !content.includes("from '@/lib/apiClient'") && !content.includes('from "@/lib/apiClient"')) {
    modified = `import { apiClient } from '@/lib/apiClient';\n${modified}`;
  }
  
  if (imports.has('useCrudOperations') && !content.includes("from '@/hooks/useCrudOperations'") && !content.includes('from "@/hooks/useCrudOperations"')) {
    modified = `import { useCrudOperations } from '@/hooks/useCrudOperations';\n${modified}`;
  }
  
  return modified;
}

// Process a single file
function processFile(filePath) {
  stats.filesScanned++;
  
  try {
    let content = fs.readFileSync(filePath, 'utf8');
    
    // Skip if no supabase references
    if (!content.includes('supabase.')) {
      stats.filesSkipped++;
      if (VERBOSE) console.log(`  ⊘ Skipped (no supabase): ${filePath}`);
      return;
    }
    
    // Skip backup files
    if (filePath.includes('.backup')) {
      stats.filesSkipped++;
      return;
    }
    
    const originalContent = content;
    const neededImports = [];
    let patternsApplied = 0;
    
    // Apply each pattern
    patterns.forEach(pattern => {
      const matches = content.match(pattern.regex);
      if (matches) {
        content = content.replace(pattern.regex, pattern.replacement);
        patternsApplied += matches.length;
        if (pattern.needsImport) {
          neededImports.push(pattern.needsImport);
        }
        if (VERBOSE) console.log(`    ✓ Applied ${pattern.name} (${matches.length} times)`);
      }
    });
    
    // If content changed, add imports and save
    if (content !== originalContent) {
      content = addImports(content, neededImports);
      
      if (!DRY_RUN) {
        // Create backup
        fs.writeFileSync(`${filePath}.backup`, originalContent);
        
        // Write modified file
        fs.writeFileSync(filePath, content);
      }
      
      stats.filesModified++;
      stats.patternsApplied += patternsApplied;
      console.log(`  ✓ Migrated: ${filePath} (${patternsApplied} patterns)`);
    } else {
      stats.filesSkipped++;
      if (VERBOSE) console.log(`  ⊘ No changes: ${filePath}`);
    }
    
  } catch (error) {
    stats.errors.push({ file: filePath, error: error.message });
    console.error(`  ✗ Error processing ${filePath}: ${error.message}`);
  }
}

// Main execution
async function main() {
  console.log('🚀 Starting Supabase to Django Migration...\n');
  
  if (DRY_RUN) {
    console.log('⚠️  DRY RUN MODE - No files will be modified\n');
  }
  
  // Find all TypeScript/TSX files
  const files = await glob('src/**/*.{ts,tsx}', { 
    ignore: ['**/*.backup.*', '**/node_modules/**', '**/dist/**', '**/build/**']
  });
  
  console.log(`Found ${files.length} files to process\n`);
  
  // Process hooks first
  console.log('📦 Processing hooks...');
  const hooks = files.filter(f => f.includes('/hooks/'));
  hooks.forEach(processFile);
  
  // Process components
  console.log('\n📦 Processing components...');
  const components = files.filter(f => f.includes('/components/') && !f.includes('/hooks/'));
  components.forEach(processFile);
  
  // Process pages
  console.log('\n📦 Processing pages...');
  const pages = files.filter(f => f.includes('/pages/'));
  pages.forEach(processFile);
  
  // Process remaining files
  console.log('\n📦 Processing other files...');
  const others = files.filter(f => !f.includes('/hooks/') && !f.includes('/components/') && !f.includes('/pages/'));
  others.forEach(processFile);
  
  // Print summary
  console.log('\n' + '='.repeat(60));
  console.log('📊 Migration Summary');
  console.log('='.repeat(60));
  console.log(`Files scanned:    ${stats.filesScanned}`);
  console.log(`Files modified:   ${stats.filesModified}`);
  console.log(`Files skipped:    ${stats.filesSkipped}`);
  console.log(`Patterns applied: ${stats.patternsApplied}`);
  console.log(`Errors:           ${stats.errors.length}`);
  
  if (stats.errors.length > 0) {
    console.log('\n❌ Errors encountered:');
    stats.errors.forEach(({ file, error }) => {
      console.log(`  - ${file}: ${error}`);
    });
  }
  
  if (!DRY_RUN && stats.filesModified > 0) {
    console.log('\n✅ Migration complete!');
    console.log('\n📝 Next steps:');
    console.log('  1. Review the changes: git diff');
    console.log('  2. Check for TypeScript errors: npm run type-check');
    console.log('  3. Test build: npm run build');
    console.log('  4. Test functionality manually');
    console.log('  5. Commit changes: git add . && git commit -m "Migrate remaining Supabase code to Django"');
    console.log('\n💡 Backup files created with .backup extension');
  } else if (DRY_RUN) {
    console.log('\n✅ Dry run complete! Run without --dry-run to apply changes.');
  } else {
    console.log('\n✅ No files needed migration.');
  }
}

// Run the script
main().catch(error => {
  console.error('Fatal error:', error);
  process.exit(1);
});
