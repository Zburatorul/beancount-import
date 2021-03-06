import * as React from "react";
import styled from "styled-components";
import { InvalidReference } from "./server_connection";
import { AssociatedDataViewController } from "./app";
import { JournalLineReference } from "./journal_errors";
import {
  ServerVirtualListComponent,
  ServerVirtualListState
} from "./server_virtual_list";

class InvalidReferenceVirtualListComponent extends ServerVirtualListComponent<
  InvalidReference
> {}

const InvalidReferencesList = styled(InvalidReferenceVirtualListComponent)`
  margin: 0;
  padding-left: 3px;
  padding-right: 3px;
  overflow-y: scroll;
  flex: 1;
  flex-basis: 0px;
`;

const InvalidReferenceElement = styled.div`
  border: 1px solid transparent;
  margin-top: 0;
  margin-bottom: 0;

  :hover {
    border: 1px solid black;
  }
`;

const InvalidReferenceFormattedElement = styled.div`
  font-family: monospace;
  white-space: pre;
`;

const JournalErrorFilename = styled.span`
  font-weight: bold;
`;

const JournalErrorLineNumber = styled.span`
  font-weight: bold;
`;

const JournalErrorMessage = styled.span`
  margin-left: 1em;
  color: red;
`;

interface InvalidReferencesComponentProps {
  listState: ServerVirtualListState<InvalidReference>;
}

interface InvalidReferenceComponentProps {
  entry: InvalidReference;
}

export class InvalidReferenceComponent extends React.PureComponent<
  InvalidReferenceComponentProps
> {
  render() {
    const { entry } = this.props;
    const firstPair = entry.transaction_posting_pairs[0];
    const formattedText =
      firstPair.posting_formatted || firstPair.transaction_formatted;
    const summaryLines = formattedText.split("\n");
    summaryLines.length = Math.min(summaryLines.length, 4);
    const summary = summaryLines.join("\n");
    return (
      <InvalidReferenceElement>
        <InvalidReferenceFormattedElement>
          {summary}
        </InvalidReferenceFormattedElement>
        {entry.source}: {entry.num_extras} occurrences more than expected:
        <ol>
          {entry.transaction_posting_pairs.map((p, i) => {
            const meta = (p.posting || p.transaction).meta!;
            return (
              <li key={i}>
                <JournalLineReference meta={meta} />
              </li>
            );
          })}
        </ol>
      </InvalidReferenceElement>
    );
  }
}

export class InvalidReferencesComponent extends React.PureComponent<
  InvalidReferencesComponentProps
> {
  private renderItem(
    entry: InvalidReference,
    index: number,
    ref: React.RefObject<any>
  ) {
    return <InvalidReferenceComponent key={index} ref={ref} entry={entry} />;
  }

  render() {
    return (
      <InvalidReferencesList
        listState={this.props.listState}
        renderItem={this.renderItem}
      />
    );
  }
}
