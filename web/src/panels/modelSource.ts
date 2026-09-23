/**
 * How the resolved model's provenance is described in the UI.
 *
 * `EffectiveModel.source` is the server's own vocabulary
 * (`app-config`, `settings-default`, `env-fallback`, `bundle-default`,
 * `graph-override`), and quoting it raw would tell a new user nothing while
 * implying they should know what a "settings-default" is. This maps each
 * source to a phrase that says where the choice came from *in this
 * application's terms* -- and keeps the inherited/environment sources honest
 * about being the advanced path rather than presenting them as required.
 *
 * Shared by every surface that shows provenance (the compile panel's model
 * line and the settings screen's footer), so two places cannot describe the
 * same source two different ways.
 */
export function modelSourceLabel(source: string): string {
  switch (source) {
    case 'app-config':
      return 'set in Model settings';
    case 'graph-override':
      return 'this graph’s override';
    case 'settings-default':
      return 'from an inherited machine-wide configuration';
    case 'env-fallback':
      return 'from the server environment';
    case 'bundle-default':
      return 'the built-in offline default (nothing configured yet)';
    default:
      return source;
  }
}
