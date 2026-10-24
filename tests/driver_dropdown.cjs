const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const source = fs.readFileSync('src/VersionDropdown.tsx', 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions: {
  module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2020,
}}).outputText;
const exportsObject = {};
vm.runInNewContext(compiled, {exports: exportsObject, console, require(name) {
  if (name === 'react') return {useEffect() {}, useState(initial) {
    return [typeof initial === 'function' ? initial() : initial, () => {}];
  }};
  if (name === 'react/jsx-runtime') return {jsx(type, props) { return {type, props}; }};
  if (name === '@moi952/decky-ui-kit') return {AnchoredDropdown: 'dropdown'};
  if (name === './ContextHelp') return {helpProps() { return {}; }};
  throw Error(name);
}});
const props = {
  versions: ['615.78.08', '610.57.04'], detailedVersions: ['615.78.08-1', '610.57.04-1'],
  showBuildNumbers: false, repoInfo: {stable:'615.78.08-1', with_testing:'615.78.08-1', testing_versions:[]},
  latestVersion: '615.78.08-1', nvidiaVersions: {'610.57.04':{category:'beta'}, '615.71.09':{category:'nfb'}, '610.43.02':{category:'recommended'}},
  channelFilters:{recommended:true,nfb:true,beta:true,nvidia_other:true,tocompile:true}, defaultChannel:'all',
  categoryLabels:{recommended:'Recommended',nfb:'NFB',beta:'Beta',nvidia_other:'Other'}, archTestingLabel:'Arch testing',
  customBuildVersions:['610.57.04'], customBuildLabel:'Compiled',
  buildableVersions:['615.78.08','615.71.09','610.57.04','610.43.02'], needsBuildLabel:'Needs build',
  currentVersion:'615.78.08-1', onSelectionChange() {},
};
const dropdown = exportsObject.VersionDropdown(props).props.children.props;
const options = dropdown.options;
assert.equal(dropdown.maxVisibleOptions, 8);
assert.equal(options.find(v => v.value === '610.57.04').label, '610.57.04 (Arch)');
assert.equal(options.filter(v => v.value === '610.57.04').length, 1);
assert.equal(options.find(v => v.value === '615.71.09').label, '615.71.09 (NVIDIA · NFB · Needs build)');
assert.equal(options.map(v => v.value).join(','), '615.78.08,615.71.09,610.57.04,610.43.02');
assert(!options.some(v => v.label.includes('Recommended')));
const filtered = exportsObject.VersionDropdown({...props, channelFilters:{...props.channelFilters, tocompile:false}}).props.children.props.options;
assert(filtered.some(v => v.value === '615.71.09'));
assert.equal(options.find(v => v.value === '610.43.02').label, '610.43.02 (NVIDIA · Other · Needs build)');
const render = (overrides) => exportsObject.VersionDropdown({...props,...overrides}).props.children.props;
const archOnly = render({channelFilters:{recommended:true,nfb:false,beta:false,nvidia_other:false,tocompile:true}});
assert.equal(archOnly.options.map(v=>v.value).join(','),'615.78.08,610.57.04');
const nfbOnly = render({channelFilters:{recommended:false,nfb:true,beta:false,nvidia_other:false,tocompile:true}});
assert.equal(nfbOnly.options.map(v=>v.value).join(','),'615.71.09');
assert.equal(nfbOnly.selectedValue,'615.71.09');
const otherOnly = render({channelFilters:{recommended:false,nfb:false,beta:false,nvidia_other:true,tocompile:true}});
assert.equal(otherOnly.options.map(v=>v.value).join(','),'610.43.02');
const empty = render({channelFilters:{recommended:false,nfb:false,beta:false,nvidia_other:false,tocompile:true}});
assert.equal(empty.options.length,0);
assert.equal(empty.selectedValue,'');
const latest = channel => exportsObject.computeChannelLatest(channel,props.detailedVersions,props.nvidiaVersions,props.repoInfo.stable,props.buildableVersions);
assert.equal(latest('recommended'),'615.78.08-1');
assert.equal(latest('nfb'),'615.71.09');
assert.equal(latest('beta'),'');
assert.equal(latest('nvidia_other'),'610.43.02');
const counts = exportsObject.channelCounts(props.detailedVersions, props.buildableVersions, props.customBuildVersions, props.nvidiaVersions);
assert.equal(JSON.stringify(counts), JSON.stringify({recommended:2,nfb:1,beta:0,nvidia_other:1}));
const betaOnly = render({
  buildableVersions:['595.45.04'], nvidiaVersions:{'595.45.04':{category:'beta'}},
  channelFilters:{recommended:false,nfb:false,beta:true,nvidia_other:false,tocompile:false},
});
assert.equal(betaOnly.options[0].label, '595.45.04 (NVIDIA · Beta · Needs build)');
assert.equal(betaOnly.selectedValue, '595.45.04');
console.log('Dropdown verified: Arch recommendation source, NVIDIA channels, source labels, no duplicates, order, filters, empty selection and update channel.');
