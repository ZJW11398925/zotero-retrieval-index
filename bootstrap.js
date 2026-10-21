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
