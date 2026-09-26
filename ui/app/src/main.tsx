import './fonts';
import './styles/index.css';

import { render } from 'preact';

import { App } from './app';

const root = document.getElementById('app');
if (!root) throw new Error('找不到 #app 掛載點');
render(<App />, root);
