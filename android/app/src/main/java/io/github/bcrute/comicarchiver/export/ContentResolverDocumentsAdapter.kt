package io.github.bcrute.comicarchiver.export

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document.MIME_TYPE_DIR
import java.io.FileNotFoundException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/**
 * [DocumentsAdapter] over the Android framework's Storage Access Framework, using only
 * ContentResolver + DocumentsContract (no documentfile dependency). Everything is a
 * document URI; no POSIX file operations touch the user's tree.
 *
 * Existing CBZ copies have manual device evidence. Recovery and provider variations
 * still need system integration coverage; pure seam tests do not establish that proof.
 */
class ContentResolverDocumentsAdapter(private val resolver: ContentResolver) : DocumentsAdapter {

    constructor(context: Context) : this(context.contentResolver)

    override fun belongsToTree(file: DocumentsFile, target: SafExportTarget): Boolean = try {
        val document = Uri.parse(file.uriString)
        val tree = Uri.parse(target.treeUriString)
        document.scheme == "content" && tree.scheme == "content" && document.authority == tree.authority &&
            DocumentsContract.getTreeDocumentId(document) == DocumentsContract.getTreeDocumentId(tree)
    } catch (_: IllegalArgumentException) { false }

    override fun rootFolder(target: SafExportTarget): DocumentsFolder {
        val tree = parseTree(target)
        val root = DocumentsContract.buildDocumentUriUsingTree(
            tree, DocumentsContract.getTreeDocumentId(tree),
        )
        // Resolve the root document; this is the cheap probe that surfaces a revoked
        // persisted grant as DocumentsException(GRANT_LOST).
        queryName(DocumentsFolder(root.toString()))
        return DocumentsFolder(root.toString())
    }

    override fun ensureSubfolder(parent: DocumentsFolder, displayName: String): DocumentsFolder {
        val parentUri = parseDoc(parent.uriString)
        val childrenUri = DocumentsContract.buildChildDocumentsUriUsingTree(
            parentUri, DocumentsContract.getDocumentId(parentUri),
        )
        try { resolver.query(
            childrenUri,
            arrayOf(
                DocumentsContract.Document.COLUMN_DOCUMENT_ID,
                DocumentsContract.Document.COLUMN_DISPLAY_NAME,
                DocumentsContract.Document.COLUMN_MIME_TYPE,
            ),
            null, null, null,
        )?.use { cursor ->
            while (cursor.moveToNext()) {
                val isDir = cursor.getString(2) == MIME_TYPE_DIR
                if (isDir && cursor.getString(1) == displayName) {
                    val child = DocumentsContract.buildDocumentUriUsingTree(
                        parentUri, cursor.getString(0),
                    )
                    return DocumentsFolder(child.toString())
                }
            }
        } } catch (e: SecurityException) { throw grantLost(e) }
          catch (e: IOException) { throw DocumentsException(DocumentsCode.IO, "folder query failed", e) }
        val created = try {
            DocumentsContract.createDocument(resolver, parentUri, MIME_TYPE_DIR, displayName)
        } catch (e: SecurityException) {
            throw grantLost(e)
        }
        return created?.let { DocumentsFolder(it.toString()) }
            ?: throw DocumentsException(DocumentsCode.TARGET_REFUSED, "subfolder refused")
    }

    override fun createFile(
        parent: DocumentsFolder,
        displayName: String,
        mimeType: String,
    ): DocumentsFile {
        val uri = try {
            DocumentsContract.createDocument(resolver, parseDoc(parent.uriString), mimeType, displayName)
        } catch (e: SecurityException) {
            throw grantLost(e)
        }
        return uri?.let { DocumentsFile(it.toString()) }
            ?: throw DocumentsException(DocumentsCode.TARGET_REFUSED, "create refused")
    }

    override fun displayNameFor(file: DocumentsFile): String =
        queryColumn(file.uriString, DocumentsContract.Document.COLUMN_DISPLAY_NAME)
            ?: throw DocumentsException(DocumentsCode.NOT_FOUND, "no display name")

    override fun sizeBytes(file: DocumentsFile): Long =
        queryColumn(file.uriString, DocumentsContract.Document.COLUMN_SIZE)?.toLongOrNull() ?: -1L

    override fun openOutput(file: DocumentsFile): OutputStream = try {
        resolver.openOutputStream(parseDoc(file.uriString), "w")
            ?: throw DocumentsException(DocumentsCode.TARGET_REFUSED, "null output stream")
    } catch (e: SecurityException) {
        throw grantLost(e)
    } catch (e: FileNotFoundException) {
        throw DocumentsException(DocumentsCode.NOT_FOUND, "target missing", e)
    }

    override fun openInput(file: DocumentsFile): InputStream = try {
        resolver.openInputStream(parseDoc(file.uriString))
            ?: throw DocumentsException(DocumentsCode.TARGET_REFUSED, "null input stream")
    } catch (e: SecurityException) {
        throw grantLost(e)
    } catch (e: FileNotFoundException) {
        throw DocumentsException(DocumentsCode.NOT_FOUND, "target missing", e)
    }

    override fun delete(file: DocumentsFile): Boolean = try {
        DocumentsContract.deleteDocument(resolver, parseDoc(file.uriString))
    } catch (e: SecurityException) {
        false
    } catch (e: IllegalArgumentException) {
        false
    } catch (e: IOException) {
        false
    }

    // -- helpers -------------------------------------------------------------

    private fun parseTree(target: SafExportTarget): Uri = try {
        val uri = Uri.parse(target.treeUriString)
        requireNotNull(DocumentsContract.getTreeDocumentId(uri))
        uri
    } catch (e: Exception) {
        throw DocumentsException(DocumentsCode.NOT_FOUND, "unusable tree uri", e)
    }

    private fun parseDoc(uriString: String): Uri = try {
        Uri.parse(uriString)
    } catch (e: Exception) {
        throw DocumentsException(DocumentsCode.NOT_FOUND, "unusable document uri", e)
    }

    private fun queryName(folder: DocumentsFolder): String? =
        queryColumn(folder.uriString, DocumentsContract.Document.COLUMN_DISPLAY_NAME)

    private fun queryColumn(docUriString: String, column: String): String? {
        val uri = parseDoc(docUriString)
        return try {
            resolver.query(uri, arrayOf(column), null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else null
            }
        } catch (e: SecurityException) {
            throw grantLost(e)
        } catch (e: FileNotFoundException) {
            throw DocumentsException(DocumentsCode.NOT_FOUND, "document missing", e)
        } catch (e: IllegalArgumentException) {
            throw DocumentsException(DocumentsCode.NOT_FOUND, "query refused", e)
        } catch (e: IOException) {
            throw DocumentsException(DocumentsCode.IO, "query failed", e)
        }
    }

    private fun grantLost(cause: Throwable) =
        DocumentsException(DocumentsCode.GRANT_LOST, "tree grant revoked", cause)
}
