/* eslint-disable no-unused-vars */
/**
 * Zotero 插件引导层。核心逻辑在 content/core.js（纯函数）与 content/retrieval.js（Zotero 集成）。
 */

var RetrievalCore;
var RetrievalIndex;
var _retrievalRootURI;

function log(msg) {
	try { Zotero.debug('[retrieval-bootstrap] ' + msg); } catch (e) {}
}

async function startup({ id, version, rootURI }) {
	_retrievalRootURI = rootURI;
	log('启动 ' + id + ' v' + version);
	await Zotero.initializationPromise;

	Services.scriptloader.loadSubScript(rootURI + 'content/core.js', this);
	// 协议正文由 build.py 从 docs/agent-usage-protocol.md 生成；开发态文件可能不存在，忽略即可
	try { Services.scriptloader.loadSubScript(rootURI + 'content/protocol-text.js', this); } catch (e) { log('协议文本未打包（开发态可忽略）'); }
	Services.scriptloader.loadSubScript(rootURI + 'content/retrieval.js', this);

	if (!this.RetrievalIndex) {
		log('核心加载失败');
		return;
	}
	await this.RetrievalIndex.init({ rootURI });
}

function shutdown(data, reason) {
	try {
		if (this.RetrievalIndex) this.RetrievalIndex.shutdown();
	} catch (e) {}
	log('已卸载');
}

function install() {}
function uninstall() {}
