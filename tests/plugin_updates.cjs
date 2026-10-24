const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function evaluate(filename, dependencies, globals = {}) {
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
      target: ts.ScriptTarget.ES2020, esModuleInterop: true},
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {exports, ...globals, require(name) {
    assert(name in dependencies, `Unexpected dependency: ${name}`);
    return dependencies[name];
  }});
  return exports;
}

(async () => {
  const {configureRelease} = await import('../scripts/configure-release.mjs');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'decky-plugin-updates-'));
  try {
    fs.mkdirSync(path.join(root, 'src'));
    fs.copyFileSync('project.config.json', path.join(root, 'project.config.json'));
    for (const owner of ['moi952', '60plus']) {
      configureRelease(root, `${owner}/decky-nvidia-update`);
      const backendConfig = fs.readFileSync(path.join(root, 'project.config.json'), 'utf8');
      assert.equal(fs.readFileSync(path.join(root, 'src/project.config.json'), 'utf8'), backendConfig);
      const config = JSON.parse(backendConfig);
      assert.equal(config.githubOwner, owner);
      // A subsequent ordinary build must not reset the publishing repository.
      configureRelease(root);
      assert.equal(fs.readFileSync(path.join(root, 'project.config.json'), 'utf8'), backendConfig);

      const base = `https://github.com/${owner}/decky-nvidia-update`;
      const asset = `${base}/releases/download/v0.4.1/decky-nvidia-update-v0.4.1.zip`;
      let request;
      const toolkit = evaluate('node_modules/@moi952/decky-plugin-toolkit/dist/githubReleases.js', {}, {
        fetch: async url => {
          request = url;
          return {ok: true, json: async () => [{tag_name: 'v0.4.1', html_url: base + '/releases/tag/v0.4.1',
            assets: [{name: 'decky-nvidia-update-v0.4.1.zip', browser_download_url: asset,
              digest: 'sha256:' + 'a'.repeat(64)}]}]};
        },
      });
      const releases = evaluate('src/utils/githubReleases.ts', {
        '@decky/manifest': {version: '0.4.0'},
        '@moi952/decky-plugin-toolkit': toolkit,
        '../project.config.json': config,
      });
      const list = await releases.fetchPluginReleases();
      assert.equal(request, `https://api.github.com/repos/${owner}/decky-nvidia-update/releases?per_page=15`);
      assert.equal(list.length, 1);
      assert.equal(list[0].asset_url, asset);
      assert.equal(list[0].sha256, 'a'.repeat(64));
      assert.equal(releases.CURRENT_VERSION, '0.4.0');
      const links = evaluate('src/utils/links.ts', {'../project.config.json': config});
      assert(links.BUG_REPORT_URL.startsWith(base + '/issues/new?'));

      // Exercise the retained toolkit installer through Decky's Gaming Mode
      // opener window. This verifies the handoff, not download/extraction on hardware.
      let install;
      const installer = evaluate('node_modules/@moi952/decky-plugin-toolkit/dist/deckyInstall.js', {
        '@decky/api': {},
      }, {window: {opener: {DeckyBackend: {call: async (...args) => {install = args;}}}}});
      await installer.installPlugin(asset, 'Decky NVIDIA Update', '0.4.1', list[0].sha256,
        installer.PluginInstallType.UPDATE);
      assert.deepEqual(Array.from(install), ['utilities/install_plugin', asset,
        'Decky NVIDIA Update', '0.4.1', 'a'.repeat(64), 2]);
    }
    const before = fs.readFileSync(path.join(root, 'project.config.json'), 'utf8');
    assert.throws(() => configureRelease(root, 'https://github.com/owner/repo'));
    assert.equal(fs.readFileSync(path.join(root, 'project.config.json'), 'utf8'), before);
  } finally {
    for (const name of ['src/project.config.json', 'project.config.json']) {
      if (fs.existsSync(path.join(root, name))) fs.unlinkSync(path.join(root, name));
    }
    fs.rmdirSync(path.join(root, 'src'));
    fs.rmdirSync(root);
  }

  const toolkit = {BackHandler: 'back', UpdateHistorySection: 'history', GitHubSection: 'github',
    SupportSection: 'support', getWhatsNewVersions: () => []};
  const jsx = (type, props) => ({type, props});
  const {SettingsView} = evaluate('src/views/SettingsView.tsx', {
    react: {}, 'react/jsx-runtime': {jsx, jsxs: jsx},
    '@decky/ui': {Focusable: 'focus', PanelSection: 'section'},
    '@moi952/decky-ui-kit': {ActionButton: 'button'},
    '@moi952/decky-plugin-toolkit': toolkit,
    'react-i18next': {useTranslation: () => ({t: key => key})},
    'react-icons/fi': {FiArrowLeft: 'arrow'},
    '../utils/githubReleases': {fetchPluginReleases() {}},
    '../utils/links': {FEATURE_REQUEST_URL: '', BUG_REPORT_URL: ''},
    '../ContextHelp': {HelpScope: 'help-scope', helpProps() { return {}; }},
  });
  const types = node => !node || typeof node !== 'object' ? [] : Array.isArray(node)
    ? node.flatMap(types) : [node.type, ...types(node.props?.children)];
  assert(types(SettingsView({onBack() {}, pluginUpdateBlocked: false})).includes('github'));
  assert(!types(SettingsView({onBack() {}, pluginUpdateBlocked: true})).includes('github'));
  console.log('Plugin updates: repository configuration, release picker, Decky handoff and busy UI passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
